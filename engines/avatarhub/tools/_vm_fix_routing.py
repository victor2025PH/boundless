# -*- coding: utf-8 -*-
"""Voicemeeter 路由体检/修复：引擎不在→自动拉起；A1 未绑物理扬声器→绑定；VAIO 条未路由→接通。
默认输出是 Voicemeeter 虚拟设备时，引擎不跑=全系统无声(2026-07-04 实测事故)。
用法: python tools/_vm_fix_routing.py [--fix]   (--fix: 体检+自动修复; 无参: 只读体检)
退出码: 0=一切正常/已修复  2=修复失败
"""
import ctypes, os, subprocess, sys, time

DLL = r"C:\Program Files (x86)\VB\Voicemeeter\VoicemeeterRemote64.dll"
EXES = [r"C:\Program Files (x86)\VB\Voicemeeter\voicemeeter8x64.exe",   # Banana
        r"C:\Program Files (x86)\VB\Voicemeeter\voicemeeterpro.exe",
        r"C:\Program Files (x86)\VB\Voicemeeter\voicemeeter.exe"]        # 标准版
SPEAKER_WDM = os.environ.get("VM_SPEAKER_NAME", "扬声器 (Realtek High Definition Audio)")


def main() -> int:
    fix = "--fix" in sys.argv
    vm = ctypes.CDLL(DLL)
    r = vm.VBVMR_Login()
    time.sleep(0.3)
    repaired = []

    if r == 1:                                   # API 可用但进程没跑 → 拉起
        if not fix:
            print("STATE engine=DOWN"); vm.VBVMR_Logout(); return 2
        exe = next((p for p in EXES if os.path.exists(p)), None)
        if not exe:
            print("FAIL 找不到 voicemeeter 可执行文件"); vm.VBVMR_Logout(); return 2
        subprocess.Popen([exe], creationflags=0x08000008)   # DETACHED|NO_WINDOW
        for _ in range(20):
            time.sleep(0.5)
            if vm.VBVMR_IsParametersDirty() >= 0 and vm.VBVMR_Login() != 1:
                break
        repaired.append("engine_started")
        time.sleep(2)

    def getf(name):
        v = ctypes.c_float(0)
        return v.value if vm.VBVMR_GetParameterFloat(name.encode(), ctypes.byref(v)) == 0 else None

    def gets(name):
        buf = ctypes.create_string_buffer(512)
        return buf.value.decode("mbcs", "replace") if vm.VBVMR_GetParameterStringA(name.encode(), buf) == 0 else ""

    def setf(name, val):
        vm.VBVMR_SetParameterFloat(name.encode(), ctypes.c_float(float(val)))

    def sets(name, val):
        vm.VBVMR_SetParameterStringA(name.encode(), val.encode("mbcs", "replace"))

    vtype = ctypes.c_long(0)
    vm.VBVMR_GetVoicemeeterType(ctypes.byref(vtype))
    n_strip = {1: 3, 2: 5, 3: 8}.get(vtype.value, 3)
    vaio = n_strip - 1 if vtype.value == 1 else 3            # 标准版 Strip[2]=VAIO; Banana Strip[3]

    bus_dev = (gets("Bus[0].device.name") or "").strip()
    if not bus_dev:
        if fix:
            sets("Bus[0].device.wdm", SPEAKER_WDM); time.sleep(2)
            bus_dev = (gets("Bus[0].device.name") or "").strip()
            repaired.append("bus0_bound")
        else:
            print("STATE bus0=UNBOUND"); vm.VBVMR_Logout(); return 2
    ok = bool(bus_dev)

    if getf(f"Strip[{vaio}].A1") != 1.0:
        if fix:
            setf(f"Strip[{vaio}].A1", 1); repaired.append("vaio_A1_on")
        else:
            ok = False
    if getf(f"Strip[{vaio}].mute") == 1.0:
        if fix:
            setf(f"Strip[{vaio}].mute", 0); repaired.append("vaio_unmute")
        else:
            ok = False
    if getf("Bus[0].mute") == 1.0:
        if fix:
            setf("Bus[0].mute", 0); repaired.append("bus0_unmute")
        else:
            ok = False
    if (getf("Bus[0].gain") or 0) < -20 and fix:
        setf("Bus[0].gain", 0); repaired.append("bus0_gain")
    time.sleep(0.8)
    final_ok = bool((gets("Bus[0].device.name") or "").strip()) and getf(f"Strip[{vaio}].A1") == 1.0
    vm.VBVMR_Logout()
    print(f"OK bus0={bus_dev!r} repaired={repaired}" if final_ok else f"FAIL repaired={repaired}")
    return 0 if final_ok else 2


if __name__ == "__main__":
    sys.exit(main())
