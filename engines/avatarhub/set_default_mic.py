# -*- coding: utf-8 -*-
"""
把 Windows 系统默认「录音设备」(含通讯默认)切到指定设备 —— 一键通话模式的关键一步。
微信/QQ 等通话 App 走系统默认麦收音；把默认麦切到 CABLE Output 后，
对方听到的就是我们写进 CABLE Input 的克隆音，无需在 App 里手动选设备。

用法：
    python set_default_mic.py                 # 默认切到 "CABLE Output"
    python set_default_mic.py "Logitech BRIO" # 切回物理麦(还原)

输出：单行 JSON {"ok": true, "device": "...", "prev": "..."}
注意：必须在"干净的新进程"里运行(comtypes 需要先于其他 COM 用户初始化)，
      live_interpreter 通过 subprocess 调用本脚本，正是为此。
"""
import sys, json, ctypes
from ctypes import POINTER, c_wchar_p, c_uint

import comtypes
import comtypes.client
from comtypes import GUID, COMMETHOD, HRESULT, IUnknown

MMDeviceEnumCLSID = GUID('{BCDE0395-E52F-467C-8E3D-C4579291692E}')
PolicyConfigVistaCLSID = GUID('{294935CE-F637-4E7C-A41B-AB255460B862}')


class IMMDevice(IUnknown):
    _iid_ = GUID('{D666063F-1587-4E43-81F1-B948E807363F}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'Activate', (['in'], POINTER(GUID)), (['in'], c_uint),
                  (['in'], ctypes.c_void_p), (['out'], POINTER(ctypes.c_void_p))),
        COMMETHOD([], HRESULT, 'OpenPropertyStore', (['in'], c_uint), (['out'], POINTER(ctypes.c_void_p))),
        COMMETHOD([], HRESULT, 'GetId', (['out'], POINTER(c_wchar_p))),
        COMMETHOD([], HRESULT, 'GetState', (['out'], POINTER(c_uint))),
    ]


class IMMDeviceCollection(IUnknown):
    _iid_ = GUID('{0BD7A1BE-7A1A-44DB-8397-CC5392387B5E}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'GetCount', (['out'], POINTER(c_uint))),
        COMMETHOD([], HRESULT, 'Item', (['in'], c_uint), (['out'], POINTER(POINTER(IMMDevice)))),
    ]


class IMMDeviceEnumerator(IUnknown):
    _iid_ = GUID('{A95664D2-9614-4F35-A746-DE8DB63617E6}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'EnumAudioEndpoints', (['in'], c_uint), (['in'], c_uint),
                  (['out'], POINTER(POINTER(IMMDeviceCollection)))),
        COMMETHOD([], HRESULT, 'GetDefaultAudioEndpoint', (['in'], c_uint), (['in'], c_uint),
                  (['out'], POINTER(POINTER(IMMDevice)))),
    ]


class IPolicyConfigVista(IUnknown):
    _iid_ = GUID('{568b9108-44bf-40b4-9006-86afe5b5a620}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'GetMixFormat', (['in'], c_wchar_p), (['out'], POINTER(ctypes.c_void_p))),
        COMMETHOD([], HRESULT, 'GetDeviceFormat', (['in'], c_wchar_p), (['in'], ctypes.c_int),
                  (['out'], POINTER(ctypes.c_void_p))),
        COMMETHOD([], HRESULT, 'SetDeviceFormat', (['in'], c_wchar_p), (['in'], ctypes.c_void_p),
                  (['in'], ctypes.c_void_p)),
        COMMETHOD([], HRESULT, 'GetProcessingPeriod', (['in'], c_wchar_p), (['in'], ctypes.c_int),
                  (['out'], POINTER(ctypes.c_longlong)), (['out'], POINTER(ctypes.c_longlong))),
        COMMETHOD([], HRESULT, 'SetProcessingPeriod', (['in'], c_wchar_p), (['in'], POINTER(ctypes.c_longlong))),
        COMMETHOD([], HRESULT, 'GetShareMode', (['in'], c_wchar_p), (['out'], POINTER(ctypes.c_void_p))),
        COMMETHOD([], HRESULT, 'SetShareMode', (['in'], c_wchar_p), (['in'], ctypes.c_void_p)),
        COMMETHOD([], HRESULT, 'GetPropertyValue', (['in'], c_wchar_p), (['in'], ctypes.c_void_p),
                  (['out'], POINTER(ctypes.c_void_p))),
        COMMETHOD([], HRESULT, 'SetPropertyValue', (['in'], c_wchar_p), (['in'], ctypes.c_void_p),
                  (['in'], ctypes.c_void_p)),
        COMMETHOD([], HRESULT, 'SetDefaultEndpoint', (['in'], c_wchar_p, 'wszDeviceId'), (['in'], c_uint, 'role')),
        COMMETHOD([], HRESULT, 'SetEndpointVisibility', (['in'], c_wchar_p), (['in'], ctypes.c_int)),
    ]


def _friendly_name(dev_id: str) -> str:
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r'SYSTEM\CurrentControlSet\Enum\SWD\MMDEVAPI' + '\\' + dev_id,
                             0, winreg.KEY_READ)
        try:
            return winreg.QueryValueEx(key, 'FriendlyName')[0]
        finally:
            winreg.CloseKey(key)
    except OSError:
        return dev_id


def main():
    target_sub = (sys.argv[1] if len(sys.argv) > 1 else "CABLE Output").lower()
    enum = comtypes.client.CreateObject(MMDeviceEnumCLSID, interface=IMMDeviceEnumerator)
    eCapture, DEVICE_STATE_ACTIVE = 1, 1
    col = enum.EnumAudioEndpoints(eCapture, DEVICE_STATE_ACTIVE)
    target_id, target_nm = None, ""
    for i in range(col.GetCount()):
        did = col.Item(i).GetId()
        nm = _friendly_name(did)
        if target_sub in nm.lower():
            target_id, target_nm = did, nm
            break
    prev = _friendly_name(enum.GetDefaultAudioEndpoint(eCapture, 0).GetId())
    if not target_id:
        print(json.dumps({"ok": False, "error": f"未找到匹配 {target_sub!r} 的录音设备", "prev": prev},
                         ensure_ascii=False))
        return 1
    pc = comtypes.client.CreateObject(PolicyConfigVistaCLSID, interface=IPolicyConfigVista)
    for role in (0, 1, 2):        # console / multimedia / communications
        pc.SetDefaultEndpoint(target_id, role)
    now = _friendly_name(enum.GetDefaultAudioEndpoint(eCapture, 0).GetId())
    print(json.dumps({"ok": target_nm == now, "device": now, "prev": prev}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
