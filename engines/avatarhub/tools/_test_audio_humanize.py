# -*- coding: utf-8 -*-
"""P0 设备人话化分类器单测：还原截图机器的 91/60 设备形态（MME 截断/多 hostapi 重复/
虚拟军团），断言 去重合并/分组/危险标记/推荐 全部符合方案。纯字符串逻辑，无音频依赖。"""
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import device_enum as de

FAIL = []
def ok(msg): print(f"  ✓ {msg}")
def ng(msg): FAIL.append(msg); print(f"  ✗ {msg}")
def check(cond, msg):
    (ok if cond else ng)(msg)

# ── 模拟截图机器的输入设备（MME 31 字符截断 + DS/WASAPI/WDM-KS 重复）──
INPUTS = [
    "Microsoft 声音映射器 - Input (MME)",
    "麦克风 (PD100X Podcast Microphone (MME)",          # MME 截断(括号没闭合)
    "麦克风 (Logitech BRIO) (MME)",
    "CABLE Output (VB-Audio Virtual  (MME)",            # MME 截断
    "Voicemeeter Out B3 (VB-Audio Vo (MME)",            # MME 截断
    "Voicemeeter Out A3 (VB-Audio Vo (MME)",
    "麦克风 (e2eSoft iVCam) (MME)",
    "Voicemeeter Out B1 (VB-Audio Vo (MME)",
    "Voicemeeter Out A2 (VB-Audio Vo (MME)",
    "麦克风 (SplitCam Audio Mixer) (MME)",
    "Voicemeeter Out A5 (VB-Audio Vo (MME)",
    "Voicemeeter Out A1 (VB-Audio Vo (MME)",
    "麦克风 (DroidCam Virtual Audio) (MME)",
    "Voicemeeter Out A4 (VB-Audio Vo (MME)",
    "立体声混音 (Realtek High Definiti (MME)",           # MME 截断
    "Voicemeeter Out B2 (VB-Audio Vo (MME)",
    "麦克风 (Realtek High Definition  (MME)",            # MME 截断
    "主声卡驱动程序 (Windows DirectSound)",
    "麦克风 (PD100X Podcast Microphone) (Windows DirectSound)",
    "CABLE Output (VB-Audio Virtual Cable) (Windows DirectSound)",
    "麦克风 (DroidCam Virtual Audio) (Windows DirectSound)",
    "立体声混音 (Realtek High Definition Audio) (Windows DirectSound)",
    "麦克风 (Realtek High Definition Audio) (Windows DirectSound)",
    "麦克风 (Logitech BRIO) (Windows DirectSound)",
    "麦克风 (PD100X Podcast Microphone) (Windows WASAPI)",
    "CABLE Output (VB-Audio Virtual Cable) (Windows WASAPI)",
    "麦克风 (DroidCam Virtual Audio) (Windows WASAPI)",
    "立体声混音 (Realtek High Definition Audio) (Windows WASAPI)",
    "麦克风 (Realtek High Definition Audio) (Windows WASAPI)",
    "麦克风 (Logitech BRIO) (Windows WASAPI)",
    "LOOPBACK:扬声器 (Realtek High Definition Audio) (Windows WASAPI)",
    "麦克风 (PD100X Podcast Microphone) (Windows WDM-KS)",
    "CABLE Output (VB-Audio Virtual Cable) (Windows WDM-KS)",
    "麦克风 (DroidCam Virtual Audio) (Windows WDM-KS)",
]
# ── 输出设备 ──
OUTPUTS = [
    "Microsoft 声音映射器 - Output (MME)",
    "扬声器 (PD100X Podcast Microphon (MME)",            # MME 截断
    "扬声器 (Realtek High Definition  (MME)",            # MME 截断
    "Voicemeeter In 5 (VB-Audio Voic (MME)",
    "CABLE In 16ch (VB-Audio Virtual (MME)",
    "Voicemeeter AUX Input (VB-Audio (MME)",
    "主声卡驱动程序 (Windows DirectSound)",
    "CABLE Input (VB-Audio Virtual C (MME)",             # MME 截断
    "Voicemeeter In 2 (VB-Audio Voic (MME)",
    "Voicemeeter Input (VB-Audio Voi (MME)",
    "Voicemeeter In 3 (VB-Audio Voic (MME)",
    "Voicemeeter In 4 (VB-Audio Voic (MME)",
    "Voicemeeter VAIO3 Input (VB-Aud (MME)",
    "耳机 (Realtek High Definition Au (MME)",            # 前面板耳机 MME 截断
    "扬声器 (PD100X Podcast Microphone) (Windows DirectSound)",
    "扬声器 (Realtek High Definition Audio) (Windows DirectSound)",
    "CABLE Input (VB-Audio Virtual Cable) (Windows DirectSound)",
    "耳机 (Realtek High Definition Audio) (Windows DirectSound)",
    "NVIDIA High Definition Audio (Windows DirectSound)",
    "扬声器 (PD100X Podcast Microphone) (Windows WASAPI)",
    "扬声器 (Realtek High Definition Audio) (Windows WASAPI)",
    "CABLE Input (VB-Audio Virtual Cable) (Windows WASAPI)",
    "耳机 (Realtek High Definition Audio) (Windows WASAPI)",
    "CABLE Input (VB-Audio Virtual Cable) (Windows WDM-KS)",
]

print("== 输入设备 人话化 ==")
ins = de.humanize_audio_devices(INPUTS, "in")
devs = ins["devices"]
byv = {d["value"]: d for d in devs}
print(f"  原始 {ins['raw_count']} 条 → 合并 {ins['merged_count']} 条")
for d in devs:
    flag = "⚠" if d["danger"] else ("·折叠" if d["hidden"] else "")
    print(f"    [{d['group']:>7}] {d['label']}  ({len(d['variants'])}条合并 {'/'.join(d['hostapis'])}) {flag}")

check(ins["raw_count"] == len(INPUTS), f"原始计数 {ins['raw_count']}")
# 关键设备逐项断言
droid = [d for d in devs if "DroidCam" in d["label"]]
check(len(droid) == 1 and droid[0]["group"] == "phone" and len(droid[0]["variants"]) == 4,
      f"DroidCam 合并 4 条 hostapi → 1 条手机麦：{droid and droid[0]['label']}")
check(droid and droid[0]["value"].endswith("(MME)"), "DroidCam value 保留 MME 原始串")
pd = [d for d in devs if d["group"] == "usb"]
check(len(pd) == 1 and "PD100X" in pd[0]["label"] and len(pd[0]["variants"]) == 4,
      f"PD100X 截断名并入全名并归独立麦：{pd and pd[0]['label']}")
brio = [d for d in devs if d["group"] == "cam"]
check(len(brio) == 1 and "BRIO" in brio[0]["label"], f"BRIO 归摄像头麦：{brio and brio[0]['label']}")
cable_out = [d for d in devs if "回收口" in d["label"]]
check(len(cable_out) == 1 and cable_out[0]["danger"] and cable_out[0]["hidden"],
      "CABLE Output 当麦=危险+默认折叠")
mix = [d for d in devs if "内放" in d["label"] and not d["label"].startswith("内放采集")]
check(len(mix) == 1 and mix[0]["danger"], "立体声混音=危险(回声)")
vm = [d for d in devs if "调音台" in d["label"]]
check(len(vm) == 8 and all(d["hidden"] for d in vm), f"Voicemeeter {len(vm)} 通道全部折叠")
lb = [d for d in devs if d["label"].startswith("内放采集")]
check(len(lb) == 1 and lb[0]["hidden"], "LOOPBACK 采集折叠")
vis = [d for d in devs if not d["hidden"]]
print(f"  默认可见 {len(vis)} 条：{[d['label'] for d in vis]}")
check(len(vis) <= 8, f"默认可见 ≤8 条（实际 {len(vis)}）")
ipick = de.pick_best(devs, "in")
check(ipick["value"] == droid[0]["value"], f"输入推荐=DroidCam(优先于 iVCam)：{ipick['label']}")

print("\n== 输出设备 人话化 ==")
outs = de.humanize_audio_devices(OUTPUTS, "out")
devs_o = outs["devices"]
print(f"  原始 {outs['raw_count']} 条 → 合并 {outs['merged_count']} 条")
for d in devs_o:
    flag = "⚠" if d["danger"] else ("·折叠" if d["hidden"] else "")
    print(f"    [{d['group']:>7}] {d['label']}  ({len(d['variants'])}条合并) {flag}")

cable_in = [d for d in devs_o if d["group"] == "live"]
check(len(cable_in) == 1 and len(cable_in[0]["variants"]) == 4 and not cable_in[0]["hidden"],
      f"CABLE Input 4 条合并 → 直播声卡组：{cable_in and cable_in[0]['label']}")
check(cable_in and cable_in[0]["value"].endswith("(MME)"), "CABLE Input value 保留 MME")
c16 = [d for d in devs_o if "16" in d["label"]]
check(len(c16) == 1 and c16[0]["hidden"], "CABLE 16ch 折叠不推荐")
hp = [d for d in devs_o if "耳机" in d["label"] and d["group"] == "monitor"]
check(len(hp) == 1 and "前面板" in hp[0]["label"], f"前面板耳机识别：{hp and hp[0]['label']}")
spk = [d for d in devs_o if "电脑音箱" in d["label"]]
check(len(spk) == 1, f"Realtek 扬声器→电脑音箱：{spk and spk[0]['label']}")
mon = [d for d in devs_o if "监听口" in d["label"]]
check(len(mon) == 1, f"PD100X 输出→麦克风自带监听口：{mon and mon[0]['label']}")
nv = [d for d in devs_o if d["group"] == "hdmi"]
check(len(nv) == 1, f"NVIDIA→显示器/HDMI：{nv and nv[0]['label']}")
vm_o = [d for d in devs_o if "调音台" in d["label"]]
check(len(vm_o) == 7 and all(d["hidden"] for d in vm_o), f"输出侧 Voicemeeter {len(vm_o)} 通道折叠")
opick = de.pick_best(devs_o, "out")
check(opick["value"] == cable_in[0]["value"], f"输出推荐=CABLE Input：{opick['label']}")
vis_o = [d for d in devs_o if not d["hidden"]]
print(f"  默认可见 {len(vis_o)} 条：{[d['label'] for d in vis_o]}")
check(len(vis_o) <= 8, f"输出默认可见 ≤8 条（实际 {len(vis_o)}）")

# 无 CABLE 时的推荐兜底
outs2 = de.humanize_audio_devices([o for o in OUTPUTS if "CABLE Input" not in o], "out")
outs2_pick = de.pick_best(outs2["devices"], "out")
check(not outs2_pick["value"] and "VB-Cable" in outs2_pick["reason"], f"无 CABLE→给安装指引：{outs2_pick['reason']}")

print("\n" + ("❌ FAIL: " + " | ".join(FAIL) if FAIL else "✅ 全部断言通过"))
sys.exit(1 if FAIL else 0)
