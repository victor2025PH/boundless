import io, sys, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
h = open(r"C:\模仿音色\logs\_p0_dom.html", encoding="utf-8", errors="replace").read()
checks = [
    ("optgroup 分组渲染", "<optgroup" in h),
    ("手机麦克风(DroidCam) 选项", "手机麦克风（DroidCam）" in h),
    ("直播声卡(CABLE Input) 选项", "直播声卡（CABLE Input）" in h),
    ("播客麦克风(PD100X) 选项", "播客麦克风（PD100X）" in h),
    ("组标签 📱手机麦克风", "📱 手机麦克风" in h),
    ("组标签 ✅直播声卡", "✅ 直播声卡（观众听这一路）" in h),
    ("跟随系统默认 空选项", "跟随系统默认" in h),
    ("常用设备计数行", re.search(r"常用设备 \d+ 个", h) is not None),
    ("推荐行(动态)", "推荐 手机麦克风（DroidCam）" in h),
    ("危险项默认不渲染(虚拟声卡回收口)", "虚拟声卡回收口" not in h),
    ("显示全部原始设备 开关", "显示全部原始设备" in h),
    ("试音按钮", "🎙 试音" in h),
    ("试听按钮", "🔊 试听" in h),
    ("声音线路卡标题", "声音线路" in h),
    ("前置检查人话名(直播虚拟声卡驱动)", "直播虚拟声卡驱动" in h),
    ("变调助记(变男声/变女声)", "变男声" in h and "变女声" in h),
    ("音色贴合度", "音色贴合度" in h),
    ("咬字保护", "咬字保护" in h),
]
bad = 0
for name, ok in checks:
    print(("  ✓ " if ok else "  ✗ ") + name)
    bad += (not ok)
m = re.search(r"常用设备 \d+ 个[^<]*", h)
if m: print("  计数行实文:", m.group(0))
m2 = re.search(r"推荐 [^<]{0,60}", h)
if m2: print("  推荐行实文:", m2.group(0))
print("FAIL" if bad else "ALL PASS")
