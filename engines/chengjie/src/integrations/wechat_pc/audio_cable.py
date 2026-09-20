# -*- coding: utf-8 -*-
"""个人微信 PC 副驾 · 语音通路（虚拟声卡，2026-09-19）。

微信 PC 4.1.9+ 的语音消息只能**现场录**：点「发语音」→ 客户端从系统默认麦克风采集 → 点「发送语音」。
没有任何「上传音频文件」的入口，所以合成音要当成麦克风信号灌进去：

    合成 OGG/WAV ──play──▶ ``CABLE Input``（render 端）══虚拟声卡══▶ ``CABLE Output``（capture 端）──▶ 微信麦克风

本模块只管这条通路，不碰 UIA（按钮在 :mod:`uia_backend`，编排在 :mod:`send_guard`）：

- :func:`find_cable` / :func:`cable_status`：VB-CABLE 在不在、两端采样率是否一致、当前默认麦克风是谁；
- :func:`align_formats`：两端采样率不一致（装完默认 Input 48k / Output 44.1k，驱动 1:1 透传 → **整体降调 8%**，
  2026-09-19 真机实锤回环 1000Hz 录成 919Hz）→ 用声音控制面板同一套未公开 COM ``IPolicyConfig::SetDeviceFormat``
  把 capture 端格式改成与 render 端一致。**不要直改注册表**（会把端点弄坏，同日踩过）；
- :func:`loopback_selftest`：播 1kHz 正弦、从 capture 端录回来看主频与电平——装机/引导页自检用；
- :class:`MicSwitch`：发送窗口内把系统默认麦克风切到 ``CABLE Output``、发完必还。切换前把「原默认」落盘，
  进程中途崩了下次启动 :func:`heal_default_mic` 还回去（否则主人所有会议软件都没声）；
- :func:`prepare_playback`：解码（soundfile，含 Opus）→ 裁首尾静音 → 重采样 → 响度归一（有声 RMS −18 dBFS，峰值封顶 −3 dBFS）→ 返回可预热的播放闭包。

依赖 ``sounddevice`` / ``soundfile`` / ``numpy`` / ``comtypes``（后者随 uiautomation 已装）；任一缺失
:func:`available` 为 False，所有入口返回「不可用」而不抛——语音是可选能力，缺了只是不发语音。
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import time
from ctypes import HRESULT, POINTER, c_int, c_void_p, c_wchar_p, wintypes
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

try:  # pragma: no cover - 环境相关
    import numpy as _np
    import sounddevice as _sd
    import soundfile as _sf
    _AUDIO_OK = True
except Exception:  # pragma: no cover
    _np = _sd = _sf = None
    _AUDIO_OK = False

try:  # pragma: no cover - 环境相关
    import comtypes as _ct
    from comtypes import COMMETHOD, GUID, IUnknown
    _COM_OK = True
except Exception:  # pragma: no cover
    _ct = None
    _COM_OK = False

#: 我们往这里播（render 端）/ 微信把这里当麦克风（capture 端）——VB-Audio 的命名就是这么反直觉
CABLE_RENDER_NAME = "CABLE Input"
CABLE_CAPTURE_NAME = "CABLE Output"
PREFERRED_HOSTAPI = "Windows WASAPI"
#: ERole：eConsole / eMultimedia / eCommunications——三个角色都切，微信用哪个都命中
ROLES = (0, 1, 2)
E_CAPTURE = 1
DEVICE_STATE_ACTIVE = 0x1
#: 微信语音硬顶 60s；留 5s 给点按/UI 延迟
WECHAT_VOICE_MAX_SEC = 55.0
#: 响度归一（2026-09-19 P2）：峰值上限 -3 dBFS（微信侧有 AGC/降噪，太满会削波），响度目标 -18 dBFS RMS
#: （对话语音典型电平；只统计有声帧，句间停顿不算）。不同 TTS 引擎/人设的稿子到对方耳朵里一样响，
#: 也不会因为电平太低被微信降噪当环境噪声吃掉。
DEFAULT_PEAK_DBFS = -3.0
DEFAULT_TARGET_RMS_DBFS = -18.0
DEFAULT_GAIN_PEAK = 10 ** (DEFAULT_PEAK_DBFS / 20.0)   # ≈0.708，峰值归一/回环自测沿用
MAX_GAIN = 6.0
#: 首尾静音裁剪：TTS 产物两头常带 150–400ms 空白，录进微信就是一段「没人说话」；低于 -50 dBFS 视为静音，
#: 两端各留 60ms 呼吸感（切太齐像被掐断）
SILENCE_TRIM_DBFS = -50.0
SILENCE_TRIM_PAD_MS = 60
#: 有声帧判定门限（帧 RMS 低于此值＝停顿，不参与响度统计）
LOUDNESS_FLOOR_DBFS = -45.0


def available() -> bool:
    return bool(_AUDIO_OK and os.name == "nt")


# ── COM：IMMDeviceEnumerator（端点 id / 友好名 / 默认端点）+ IPolicyConfig（设默认 / 设格式）──────────
if _COM_OK:  # pragma: no cover - 平台相关
    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

    class PROPVARIANT(ctypes.Structure):
        # 只用得到 VT_LPWSTR：vt + 3 个保留 WORD + 8 字节 union（x64 上 pwszVal 就在 offset 8）
        _fields_ = [("vt", wintypes.WORD), ("r1", wintypes.WORD), ("r2", wintypes.WORD), ("r3", wintypes.WORD),
                    ("pwszVal", c_wchar_p), ("pad", c_void_p)]

    class WAVEFORMATEX(ctypes.Structure):
        _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD), ("nSamplesPerSec", wintypes.DWORD),
                    ("nAvgBytesPerSec", wintypes.DWORD), ("nBlockAlign", wintypes.WORD),
                    ("wBitsPerSample", wintypes.WORD), ("cbSize", wintypes.WORD)]

    class IPropertyStore(IUnknown):
        _iid_ = GUID("{886d8eeb-8cf2-4446-8d02-cdba1dbdcf99}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(wintypes.DWORD), "n")),
            COMMETHOD([], HRESULT, "GetAt", (["in"], wintypes.DWORD, "i"), (["out"], POINTER(PROPERTYKEY), "key")),
            COMMETHOD([], HRESULT, "GetValue", (["in"], POINTER(PROPERTYKEY), "key"),
                      (["out"], POINTER(PROPVARIANT), "pv")),
            COMMETHOD([], HRESULT, "SetValue", (["in"], POINTER(PROPERTYKEY), "key"),
                      (["in"], POINTER(PROPVARIANT), "pv")),
            COMMETHOD([], HRESULT, "Commit"),
        ]

    class IMMDevice(IUnknown):
        _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
        _methods_ = [
            COMMETHOD([], HRESULT, "Activate", (["in"], POINTER(GUID), "iid"), (["in"], wintypes.DWORD, "clsctx"),
                      (["in"], c_void_p, "params"), (["out"], POINTER(POINTER(IUnknown)), "iface")),
            COMMETHOD([], HRESULT, "OpenPropertyStore", (["in"], wintypes.DWORD, "access"),
                      (["out"], POINTER(POINTER(IPropertyStore)), "store")),
            COMMETHOD([], HRESULT, "GetId", (["out"], POINTER(c_wchar_p), "id")),
            COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(wintypes.DWORD), "state")),
        ]

    class IMMDeviceCollection(IUnknown):
        _iid_ = GUID("{0BD7A1BE-7A1A-44DB-8397-CC5392387B5E}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(wintypes.UINT), "n")),
            COMMETHOD([], HRESULT, "Item", (["in"], wintypes.UINT, "i"), (["out"], POINTER(POINTER(IMMDevice)), "dev")),
        ]

    class IMMDeviceEnumerator(IUnknown):
        _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        _methods_ = [
            COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], c_int, "dataFlow"), (["in"], wintypes.DWORD, "mask"),
                      (["out"], POINTER(POINTER(IMMDeviceCollection)), "devices")),
            COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (["in"], c_int, "dataFlow"), (["in"], c_int, "role"),
                      (["out"], POINTER(POINTER(IMMDevice)), "endpoint")),
            COMMETHOD([], HRESULT, "GetDevice", (["in"], c_wchar_p, "id"), (["out"], POINTER(POINTER(IMMDevice)), "dev")),
        ]

    class IPolicyConfig(IUnknown):
        _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetMixFormat", (["in"], c_wchar_p, "id"),
                      (["out"], POINTER(POINTER(WAVEFORMATEX)), "fmt")),
            COMMETHOD([], HRESULT, "GetDeviceFormat", (["in"], c_wchar_p, "id"), (["in"], c_int, "bDefault"),
                      (["out"], POINTER(POINTER(WAVEFORMATEX)), "fmt")),
            COMMETHOD([], HRESULT, "ResetDeviceFormat", (["in"], c_wchar_p, "id")),
            COMMETHOD([], HRESULT, "SetDeviceFormat", (["in"], c_wchar_p, "id"), (["in"], POINTER(WAVEFORMATEX), "ep"),
                      (["in"], POINTER(WAVEFORMATEX), "mix")),
            COMMETHOD([], HRESULT, "GetProcessingPeriod", (["in"], c_wchar_p, "id"), (["in"], c_int, "bDefault"),
                      (["out"], POINTER(ctypes.c_int64), "a"), (["out"], POINTER(ctypes.c_int64), "b")),
            COMMETHOD([], HRESULT, "SetProcessingPeriod", (["in"], c_wchar_p, "id"), (["in"], POINTER(ctypes.c_int64), "a")),
            COMMETHOD([], HRESULT, "GetShareMode", (["in"], c_wchar_p, "id"), (["out"], POINTER(c_int), "mode")),
            COMMETHOD([], HRESULT, "SetShareMode", (["in"], c_wchar_p, "id"), (["in"], POINTER(c_int), "mode")),
            COMMETHOD([], HRESULT, "GetPropertyValue", (["in"], c_wchar_p, "id"), (["in"], c_void_p, "key"),
                      (["out"], c_void_p, "pv")),
            COMMETHOD([], HRESULT, "SetPropertyValue", (["in"], c_wchar_p, "id"), (["in"], c_void_p, "key"),
                      (["in"], c_void_p, "pv")),
            COMMETHOD([], HRESULT, "SetDefaultEndpoint", (["in"], c_wchar_p, "id"), (["in"], c_int, "role")),
            COMMETHOD([], HRESULT, "SetEndpointVisibility", (["in"], c_wchar_p, "id"), (["in"], c_int, "visible")),
        ]

    # ── 会话枚举（谁在用麦克风）：IMMDevice::Activate(IAudioSessionManager2) → 每个会话 GetState/GetProcessId ──
    class IAudioSessionControl(IUnknown):
        _iid_ = GUID("{F4B1A599-7266-4319-A8CA-E70ACB11E8CD}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(c_int), "state")),
            COMMETHOD([], HRESULT, "GetDisplayName", (["out"], POINTER(c_wchar_p), "name")),
            COMMETHOD([], HRESULT, "SetDisplayName", (["in"], c_wchar_p, "name"), (["in"], POINTER(GUID), "ctx")),
            COMMETHOD([], HRESULT, "GetIconPath", (["out"], POINTER(c_wchar_p), "path")),
            COMMETHOD([], HRESULT, "SetIconPath", (["in"], c_wchar_p, "path"), (["in"], POINTER(GUID), "ctx")),
            COMMETHOD([], HRESULT, "GetGroupingParam", (["out"], POINTER(GUID), "g")),
            COMMETHOD([], HRESULT, "SetGroupingParam", (["in"], POINTER(GUID), "g"), (["in"], POINTER(GUID), "ctx")),
            COMMETHOD([], HRESULT, "RegisterAudioSessionNotification", (["in"], c_void_p, "ev")),
            COMMETHOD([], HRESULT, "UnregisterAudioSessionNotification", (["in"], c_void_p, "ev")),
        ]

    class IAudioSessionControl2(IAudioSessionControl):
        _iid_ = GUID("{bfb7ff88-7239-4fc9-8fa2-07c950be9c6d}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetSessionIdentifier", (["out"], POINTER(c_wchar_p), "s")),
            COMMETHOD([], HRESULT, "GetSessionInstanceIdentifier", (["out"], POINTER(c_wchar_p), "s")),
            COMMETHOD([], HRESULT, "GetProcessId", (["out"], POINTER(wintypes.DWORD), "pid")),
            COMMETHOD([], HRESULT, "IsSystemSoundsSession"),
            COMMETHOD([], HRESULT, "SetDuckingPreference", (["in"], c_int, "opt_out")),
        ]

    class IAudioSessionEnumerator(IUnknown):
        _iid_ = GUID("{E2F5BB11-0570-40CA-ACDD-3AA01277DEE8}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_int), "n")),
            COMMETHOD([], HRESULT, "GetSession", (["in"], c_int, "i"),
                      (["out"], POINTER(POINTER(IAudioSessionControl)), "s")),
        ]

    class IAudioSessionManager(IUnknown):
        _iid_ = GUID("{BFA971F1-4D5E-40BB-935E-967039BFBEE4}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetAudioSessionControl", (["in"], POINTER(GUID), "g"), (["in"], wintypes.DWORD, "f"),
                      (["out"], POINTER(POINTER(IAudioSessionControl)), "s")),
            COMMETHOD([], HRESULT, "GetSimpleAudioVolume", (["in"], POINTER(GUID), "g"), (["in"], wintypes.DWORD, "f"),
                      (["out"], POINTER(POINTER(IUnknown)), "v")),
        ]

    class IAudioSessionManager2(IAudioSessionManager):
        _iid_ = GUID("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetSessionEnumerator", (["out"], POINTER(POINTER(IAudioSessionEnumerator)), "e")),
            COMMETHOD([], HRESULT, "RegisterSessionNotification", (["in"], c_void_p, "n")),
            COMMETHOD([], HRESULT, "UnregisterSessionNotification", (["in"], c_void_p, "n")),
            COMMETHOD([], HRESULT, "RegisterDuckNotification", (["in"], c_wchar_p, "sid"), (["in"], c_void_p, "n")),
            COMMETHOD([], HRESULT, "UnregisterDuckNotification", (["in"], c_void_p, "n")),
        ]

    _CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
    _CLSID_PolicyConfigClient = GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}")
    _PKEY_Device_FriendlyName = PROPERTYKEY(GUID("{a45c254e-df1c-4efd-8020-67d146a850e0}"), 14)

#: AudioSessionState
AUDIO_SESSION_ACTIVE = 1


def _co_init() -> None:  # pragma: no cover - 平台相关
    try:
        _ct.CoInitialize()
    except Exception:
        pass  # 已由 uiautomation 初始化（可能是另一线程模型）→ 忽略


def _enumerator():  # pragma: no cover - 平台相关
    _co_init()
    return _ct.CoCreateInstance(_CLSID_MMDeviceEnumerator, interface=IMMDeviceEnumerator, clsctx=_ct.CLSCTX_ALL)


def _policy():  # pragma: no cover - 平台相关
    _co_init()
    return _ct.CoCreateInstance(_CLSID_PolicyConfigClient, interface=IPolicyConfig, clsctx=_ct.CLSCTX_ALL)


def _friendly_name(dev) -> str:  # pragma: no cover - 平台相关
    try:
        store = dev.OpenPropertyStore(0)  # STGM_READ
        pv = store.GetValue(ctypes.byref(_PKEY_Device_FriendlyName))
        try:
            return str(pv.pwszVal or "") if pv.vt == 31 else ""
        finally:
            try:
                ctypes.windll.ole32.PropVariantClear(ctypes.byref(pv))
            except Exception:
                pass
    except Exception:
        return ""


def list_capture_endpoints() -> Dict[str, str]:
    """活动的录音端点 ``{endpoint_id: friendly_name}``；COM 不可用回空。"""
    if not (_COM_OK and os.name == "nt"):
        return {}
    out: Dict[str, str] = {}
    try:  # pragma: no cover - 平台相关
        coll = _enumerator().EnumAudioEndpoints(E_CAPTURE, DEVICE_STATE_ACTIVE)
        for i in range(int(coll.GetCount())):
            dev = coll.Item(i)
            out[str(dev.GetId())] = _friendly_name(dev)
    except Exception:
        logger.debug("[audio_cable] 枚举录音端点失败", exc_info=True)
    return out


def _endpoint_id_by_name(flow: int, part: str) -> str:  # pragma: no cover - 平台相关
    try:
        coll = _enumerator().EnumAudioEndpoints(flow, DEVICE_STATE_ACTIVE)
        for i in range(int(coll.GetCount())):
            dev = coll.Item(i)
            if part in _friendly_name(dev):
                return str(dev.GetId())
    except Exception:
        logger.debug("[audio_cable] 找端点失败 %s", part, exc_info=True)
    return ""


def endpoint_friendly_name(endpoint_id: str) -> str:
    if not (_COM_OK and os.name == "nt" and endpoint_id):
        return ""
    try:  # pragma: no cover - 平台相关
        return _friendly_name(_enumerator().GetDevice(str(endpoint_id)))
    except Exception:
        return ""


def default_capture_endpoint(role: int = 0) -> str:
    """当前系统默认麦克风的端点 id（取不到回空串）。"""
    if not (_COM_OK and os.name == "nt"):
        return ""
    try:  # pragma: no cover - 平台相关
        return str(_enumerator().GetDefaultAudioEndpoint(E_CAPTURE, int(role)).GetId())
    except Exception:
        logger.debug("[audio_cable] 读默认麦克风失败", exc_info=True)
        return ""


def set_default_capture_endpoint(endpoint_id: str, roles: Tuple[int, ...] = ROLES) -> bool:
    """把系统默认麦克风切到 ``endpoint_id``（三个角色都切）。成功＝切完回读一致。"""
    if not (_COM_OK and os.name == "nt" and endpoint_id):
        return False
    try:  # pragma: no cover - 平台相关
        pc = _policy()
        for r in roles:
            pc.SetDefaultEndpoint(str(endpoint_id), int(r))
        return default_capture_endpoint(0) == str(endpoint_id)
    except Exception:
        logger.debug("[audio_cable] 设默认麦克风失败", exc_info=True)
        return False


def _process_name(pid: int) -> str:  # pragma: no cover - 平台相关
    if not pid or os.name != "nt":
        return ""
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = wintypes.DWORD(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return os.path.basename(buf.value)
        finally:
            k32.CloseHandle(h)
    except Exception:
        pass
    return ""


def capture_in_use(*, exclude_endpoint_ids: Tuple[str, ...] = (), exclude_pids: Tuple[int, ...] = ()) -> Dict[str, Any]:
    """谁在用麦克风：枚举所有活动录音端点上的音频会话，状态 Active 的就是「正在录」的进程。

    → ``{"busy": bool, "sessions": [{"endpoint", "device", "pid", "process"}], "error": ""}``。
    ``exclude_endpoint_ids``：不看的端点（我们自己的 ``CABLE Output``——微信录我们的合成音时也是 Active）；
    ``exclude_pids``：不算的进程（本进程）。COM 不可用 → ``busy=False, error="com_unavailable"``（不拦发送：
    检测不了不能当成一直被占用）。

    用途（2026-09-19 P2）：坐席正在开会/通话（Zoom/Teams/微信通话…）时**不切默认麦克风、不发语音**——跟随
    「默认设备」的软件（Chrome 系）会在切换瞬间把对方听到的声音换成我们的合成音。
    """
    if not (_COM_OK and os.name == "nt"):
        return {"busy": False, "sessions": [], "error": "com_unavailable"}
    out: Dict[str, Any] = {"busy": False, "sessions": [], "error": ""}
    try:  # pragma: no cover - 平台相关
        coll = _enumerator().EnumAudioEndpoints(E_CAPTURE, DEVICE_STATE_ACTIVE)
        for i in range(int(coll.GetCount())):
            dev = coll.Item(i)
            eid = str(dev.GetId())
            if eid in exclude_endpoint_ids:
                continue
            try:
                unk = dev.Activate(ctypes.byref(IAudioSessionManager2._iid_), _ct.CLSCTX_ALL, None)
                mgr = unk.QueryInterface(IAudioSessionManager2)
                en = mgr.GetSessionEnumerator()
            except Exception:
                logger.debug("[audio_cable] 端点会话枚举失败 %s", eid, exc_info=True)
                continue
            for j in range(int(en.GetCount())):
                try:
                    sc = en.GetSession(j)
                    if int(sc.GetState()) != AUDIO_SESSION_ACTIVE:
                        continue
                    pid = 0
                    try:
                        pid = int(sc.QueryInterface(IAudioSessionControl2).GetProcessId())
                    except Exception:
                        pid = 0
                    if pid and pid in exclude_pids:
                        continue
                    out["sessions"].append({"endpoint": eid, "device": _friendly_name(dev), "pid": pid,
                                            "process": _process_name(pid)})
                except Exception:
                    continue
        out["busy"] = bool(out["sessions"])
    except Exception as exc:  # noqa: BLE001
        logger.debug("[audio_cable] 麦克风占用检测失败", exc_info=True)
        out["error"] = f"{type(exc).__name__}"
    return out


# ── 设备发现 ─────────────────────────────────────────────────────────────────────
@dataclass
class CableInfo:
    present: bool = False
    render_index: int = -1
    capture_index: int = -1
    render_sr: int = 0
    capture_sr: int = 0
    render_endpoint_id: str = ""
    capture_endpoint_id: str = ""
    hostapi: str = ""
    error: str = ""

    @property
    def rates_match(self) -> bool:
        return self.present and self.render_sr > 0 and self.render_sr == self.capture_sr

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["rates_match"] = self.rates_match
        return d


def _pick_device(devs, apis, part: str, want_output: bool) -> Tuple[int, int, str]:
    best = (-1, 0, "")
    for i, d in enumerate(devs):
        if part not in str(d.get("name") or ""):
            continue
        chans = d.get("max_output_channels" if want_output else "max_input_channels") or 0
        if chans <= 0:
            continue
        api = str(apis[d["hostapi"]]["name"]) if 0 <= int(d["hostapi"]) < len(apis) else ""
        cand = (i, int(d.get("default_samplerate") or 0), api)
        if api == PREFERRED_HOSTAPI:
            return cand
        if best[0] < 0:
            best = cand
    return best


def find_cable() -> CableInfo:
    """找 VB-CABLE 两端（优先 WASAPI，采样率取设备默认＝共享模式格式）；顺手取两端端点 id。"""
    info = CableInfo()
    if not available():
        info.error = "audio_libs_missing" if os.name == "nt" else "not_windows"
        return info
    try:
        devs = _sd.query_devices()
        apis = _sd.query_hostapis()
    except Exception as exc:  # noqa: BLE001
        info.error = f"query_devices:{type(exc).__name__}"
        return info
    ri, rsr, rapi = _pick_device(devs, apis, CABLE_RENDER_NAME, True)
    ci, csr, _capi = _pick_device(devs, apis, CABLE_CAPTURE_NAME, False)
    if ri < 0 or ci < 0:
        info.error = "cable_not_installed"
        return info
    info.present = True
    info.render_index, info.capture_index = ri, ci
    info.render_sr, info.capture_sr = rsr, csr
    info.hostapi = rapi
    if _COM_OK:
        info.render_endpoint_id = _endpoint_id_by_name(0, CABLE_RENDER_NAME)
        info.capture_endpoint_id = _endpoint_id_by_name(E_CAPTURE, CABLE_CAPTURE_NAME)
    return info


def align_formats(info: Optional[CableInfo] = None) -> Dict[str, Any]:
    """capture 端格式 := render 端格式（IPolicyConfig::SetDeviceFormat）。需要管理员；返回 {ok, before, after}。"""
    info = info or find_cable()
    if not (info.present and info.render_endpoint_id and info.capture_endpoint_id and _COM_OK):
        return {"ok": False, "error": info.error or "endpoint_ids_missing"}
    try:  # pragma: no cover - 平台相关
        pc = _policy()
        src = pc.GetDeviceFormat(info.render_endpoint_id, 0)
        total = ctypes.sizeof(WAVEFORMATEX) + int(src.contents.cbSize)
        buf = ctypes.create_string_buffer(ctypes.string_at(src, total), total)
        pfmt = ctypes.cast(buf, POINTER(WAVEFORMATEX))
        before = int(pc.GetDeviceFormat(info.capture_endpoint_id, 0).contents.nSamplesPerSec)
        pc.SetDeviceFormat(info.capture_endpoint_id, pfmt, pfmt)
        after = int(pc.GetDeviceFormat(info.capture_endpoint_id, 0).contents.nSamplesPerSec)
        return {"ok": after == int(src.contents.nSamplesPerSec), "before": before, "after": after,
                "render_sr": int(src.contents.nSamplesPerSec)}
    except Exception as exc:  # noqa: BLE001
        logger.debug("[audio_cable] 对齐采样率失败", exc_info=True)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def loopback_selftest(info: Optional[CableInfo] = None, *, seconds: float = 1.2, freq: float = 1000.0) -> Dict[str, Any]:
    """播 ``freq`` Hz 正弦进 render 端、从 capture 端录回：主频 ±3% 且 RMS 够大才算通。"""
    info = info or find_cable()
    if not info.present:
        return {"ok": False, "error": info.error or "cable_not_installed"}
    try:
        out_sr, in_sr = info.render_sr, info.capture_sr
        t = _np.arange(int(out_sr * seconds)) / out_sr
        tone = _np.repeat((0.4 * _np.sin(2 * _np.pi * freq * t)).astype(_np.float32)[:, None], 2, axis=1)
        chunks = []
        # 录音用独立 InputStream：sounddevice 的 rec()/play() 便捷函数互斥（后者会停掉前者）
        with _sd.InputStream(samplerate=in_sr, channels=1, dtype="float32", device=info.capture_index,
                             callback=lambda indata, frames, t_, status: chunks.append(indata.copy())):
            _sd.sleep(250)
            _sd.play(tone, samplerate=out_sr, device=info.render_index, blocking=True)
            _sd.sleep(200)
        rec = _np.concatenate(chunks).flatten() if chunks else _np.zeros(0, dtype=_np.float32)
        mid = rec[int(in_sr * 0.5): int(in_sr * (seconds + 0.1))]
        if len(mid) < 64:
            return {"ok": False, "error": "too_short"}
        spec = _np.abs(_np.fft.rfft(mid * _np.hanning(len(mid))))
        freqs = _np.fft.rfftfreq(len(mid), 1 / in_sr)
        dom = float(freqs[int(_np.argmax(spec))])
        rms = float(_np.sqrt(_np.mean(mid ** 2)))
        ok = abs(dom - freq) <= freq * 0.03 and rms > 0.05
        return {"ok": ok, "rms": round(rms, 4), "dominant_hz": round(dom, 1), "expected_hz": freq,
                "render_sr": out_sr, "capture_sr": in_sr}
    except Exception as exc:  # noqa: BLE001
        logger.debug("[audio_cable] 回环自测失败", exc_info=True)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def cable_status(*, selftest: bool = False) -> Dict[str, Any]:
    """引导页 / env_check 用的一把抓：装了没、采样率齐不齐、默认麦克风是谁、（可选）回环通不通。"""
    info = find_cable()
    out: Dict[str, Any] = {"available": available(), **info.as_dict()}
    cur = default_capture_endpoint()
    out["default_mic_id"] = cur
    out["default_mic_name"] = endpoint_friendly_name(cur) if cur else ""
    out["default_mic_is_cable"] = bool(cur and info.capture_endpoint_id and cur == info.capture_endpoint_id)
    out["ready"] = bool(info.present and info.rates_match)
    if selftest and info.present:
        out["selftest"] = loopback_selftest(info)
        out["ready"] = out["ready"] and bool(out["selftest"].get("ok"))
    return out


# ── 默认麦克风短窗切换 + 自愈 ──────────────────────────────────────────────────────
def heal_default_mic(state_path: str) -> Optional[str]:
    """上次切换后没还回去（进程崩了）→ 现在还。返回还回去的端点 id；没什么可还 → None。"""
    try:
        with open(state_path, "r", encoding="utf-8") as fh:
            st = json.load(fh) or {}
    except Exception:
        return None
    prev = str(st.get("prev_default") or "")
    cable = str(st.get("cable") or "")
    restored = None
    try:
        cur = default_capture_endpoint()
        if prev and cur and cur == cable and prev != cable:
            if set_default_capture_endpoint(prev):
                restored = prev
                logger.warning("[audio_cable] 上次发送后默认麦克风未还回（进程异常退出？）→ 已还回 %s",
                               endpoint_friendly_name(prev) or prev)
    finally:
        try:
            os.remove(state_path)
        except Exception:
            pass
    return restored


class MicSwitch:
    """``with MicSwitch(info, state_path):`` 期间系统默认麦克风＝CABLE Output；退出必还。

    切换前落盘 ``{prev_default, cable, ts}``，还回后删；崩了由 :func:`heal_default_mic` 兜底。
    原默认本来就是 CABLE（用户自己设的）→ 不动、不落盘。
    """

    def __init__(self, info: CableInfo, state_path: str) -> None:
        self.info = info
        self.state_path = state_path
        self.prev = ""
        self.switched = False

    def __enter__(self) -> "MicSwitch":
        cable = self.info.capture_endpoint_id
        if not cable:
            raise RuntimeError("cable_endpoint_missing")
        self.prev = default_capture_endpoint()
        if self.prev == cable:
            return self
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as fh:
                json.dump({"prev_default": self.prev, "cable": cable, "ts": time.time()}, fh)
        except Exception:
            logger.debug("[audio_cable] 写麦克风切换状态失败", exc_info=True)
        if not set_default_capture_endpoint(cable):
            raise RuntimeError("set_default_mic_failed")
        self.switched = True
        return self

    def __exit__(self, *exc) -> None:
        if self.switched:
            ok = set_default_capture_endpoint(self.prev) if self.prev else False
            if not ok:
                logger.error("[audio_cable] 默认麦克风还回失败（prev=%s）——状态文件保留，下次启动自愈", self.prev)
                return
        try:
            os.remove(self.state_path)
        except Exception:
            pass


# ── 音频：解码 / 重采样 / 归一 / 播放 ──────────────────────────────────────────────
def load_audio(path: str):
    """→ ``(float32 [n, 2], sr)``；soundfile 解 wav/flac/ogg(vorbis/opus)。"""
    data, sr = _sf.read(path, dtype="float32", always_2d=True)
    if data.shape[1] == 1:
        data = _np.repeat(data, 2, axis=1)
    elif data.shape[1] > 2:
        data = data[:, :2]
    return data, int(sr)


def resample(x, sr_from: int, sr_to: int):
    if sr_from == sr_to or len(x) == 0:
        return x
    try:
        from math import gcd
        from scipy.signal import resample_poly  # type: ignore
        g = gcd(int(sr_from), int(sr_to))
        return resample_poly(x, int(sr_to) // g, int(sr_from) // g, axis=0).astype(_np.float32)
    except Exception:
        n_to = int(round(len(x) * sr_to / sr_from))
        t_from = _np.linspace(0.0, 1.0, len(x), endpoint=False)
        t_to = _np.linspace(0.0, 1.0, n_to, endpoint=False)
        return _np.stack([_np.interp(t_to, t_from, x[:, c]) for c in range(x.shape[1])], axis=1).astype(_np.float32)


def normalize_peak(x, peak: float = DEFAULT_GAIN_PEAK, max_gain: float = MAX_GAIN):
    p = float(_np.max(_np.abs(x))) if len(x) else 0.0
    if p <= 0:
        return x
    return _np.clip(x * min(peak / p, max_gain), -1.0, 1.0).astype(_np.float32)


def _dbfs(v: float) -> float:
    return 20.0 * _np.log10(max(float(v), 1e-9))


def trim_silence(x, sr: int, *, threshold_db: float = SILENCE_TRIM_DBFS, pad_ms: int = SILENCE_TRIM_PAD_MS):
    """裁掉首尾静音（任一声道峰值低于 ``threshold_db``），两端各留 ``pad_ms``。全静音原样返回（由上层判 too_short）。"""
    if len(x) == 0:
        return x
    thr = 10 ** (float(threshold_db) / 20.0)
    env = _np.max(_np.abs(x), axis=1)
    idx = _np.nonzero(env > thr)[0]
    if len(idx) == 0:
        return x
    pad = int(int(sr) * max(0, int(pad_ms)) / 1000)
    a = max(0, int(idx[0]) - pad)
    b = min(len(x), int(idx[-1]) + 1 + pad)
    return x[a:b]


def active_rms(x, sr: int, *, floor_db: float = LOUDNESS_FLOOR_DBFS, frame_ms: int = 20) -> float:
    """有声帧的整体 RMS（单声道混合、20ms 帧；帧 RMS 低于 ``floor_db`` 的停顿帧不算）。全静音 → 0。"""
    if len(x) == 0:
        return 0.0
    mono = _np.mean(x, axis=1).astype(_np.float32)
    frame = max(1, int(int(sr) * frame_ms / 1000))
    n = (len(mono) // frame) * frame
    if n <= 0:
        return float(_np.sqrt(_np.mean(mono ** 2)))
    frames = mono[:n].reshape(-1, frame)
    frms = _np.sqrt(_np.mean(frames ** 2, axis=1))
    floor = 10 ** (float(floor_db) / 20.0)
    active = frms[frms > floor]
    if len(active) == 0:
        return 0.0
    return float(_np.sqrt(_np.mean(active ** 2)))


def normalize_loudness(x, sr: int, *, target_rms_db: float = DEFAULT_TARGET_RMS_DBFS,
                       peak_db: float = DEFAULT_PEAK_DBFS, max_gain: float = MAX_GAIN,
                       floor_db: float = LOUDNESS_FLOOR_DBFS):
    """响度归一 → ``(y, gain_db)``：有声 RMS 拉到 ``target_rms_db``，但峰值绝不越过 ``peak_db``，增益不超 ``max_gain``。

    峰值上限优先于响度目标：稿子动态大（峭峰）就只能比目标响度小一点，而不是削波。
    """
    if len(x) == 0:
        return x, 0.0
    rms = active_rms(x, sr, floor_db=floor_db)
    peak = float(_np.max(_np.abs(x)))
    if peak <= 0:
        return x, 0.0
    if rms <= 0:
        # 整段都低于有声门限（极小声的稿子）：退化用整体 RMS，照样往上拉（受 max_gain 限）
        rms = float(_np.sqrt(_np.mean(_np.mean(x, axis=1) ** 2)))
    target = 10 ** (float(target_rms_db) / 20.0)
    gain = (target / rms) if rms > 0 else 1.0
    gain = min(gain, float(max_gain))
    ceiling = 10 ** (float(peak_db) / 20.0)
    if peak * gain > ceiling:
        gain = ceiling / peak
    y = _np.clip(x * gain, -1.0, 1.0).astype(_np.float32)
    return y, float(20.0 * _np.log10(max(gain, 1e-9)))


def audio_duration_sec(path: str) -> float:
    try:
        inf = _sf.info(path)
        return float(inf.frames) / float(inf.samplerate or 1)
    except Exception:
        return 0.0


class Playback:
    """一条已就绪的待灌音频（可调用）：``play()`` / ``play(during=fn)`` 阻塞播完返回秒数。

    - :meth:`warmup`：**进录音态之前**把输出流打开并启动（WASAPI 开流 100–300ms——不预热就全录成开头静音）；
      空闲的流吐静音，声卡那头的微信还没开录，无害。
    - ``during``：播放期间在调用线程跑的回调（send_guard 用它提前定位「发送语音」按钮——UIA 遍历几百毫秒，
      藏进播放窗口里就不算在尾部静音上）。回调异常只记日志，不影响播放。
    - :meth:`close`：收流；没 warmup 过就退化为一次性 ``sd.play``。
    """

    def __init__(self, data, samplerate: int, device: int, *, gain_db: float = 0.0, trimmed_ms: int = 0) -> None:
        self.data = data
        self.samplerate = int(samplerate)
        self.device = int(device)
        self.gain_db = float(gain_db)
        self.trimmed_ms = int(trimmed_ms)
        self._stream = None

    @property
    def duration(self) -> float:
        return len(self.data) / float(self.samplerate or 1)

    def warmup(self) -> bool:
        if self._stream is not None:
            return True
        try:
            st = _sd.OutputStream(samplerate=self.samplerate, device=self.device, channels=2, dtype="float32")
            st.start()
            self._stream = st
            return True
        except Exception:
            logger.debug("[audio_cable] 预开输出流失败，退化为一次性播放", exc_info=True)
            self._stream = None
            return False

    def close(self) -> None:
        st, self._stream = self._stream, None
        if st is None:
            return
        try:
            st.stop()
        except Exception:
            pass
        try:
            st.close()
        except Exception:
            pass

    @staticmethod
    def _run_during(during: Optional[Callable[[], Any]]) -> None:
        if during is None:
            return
        try:
            during()
        except Exception:
            logger.debug("[audio_cable] 播放期回调异常（忽略）", exc_info=True)

    def __call__(self, during: Optional[Callable[[], Any]] = None) -> float:
        st = self._stream
        if st is None:
            if during is None:
                _sd.play(self.data, samplerate=self.samplerate, device=self.device, blocking=True)
            else:
                _sd.play(self.data, samplerate=self.samplerate, device=self.device, blocking=False)
                self._run_during(during)
                _sd.wait()
            return self.duration
        import threading
        err: list = []

        def _write() -> None:
            try:
                st.write(self.data)
            except Exception as exc:  # noqa: BLE001
                err.append(exc)

        th = threading.Thread(target=_write, name="wxpc-voice-play", daemon=True)
        th.start()
        self._run_during(during)
        th.join()
        if err:
            raise err[0]
        return self.duration


def prepare_playback(path: str, info: CableInfo, *, target_rms_db: float = DEFAULT_TARGET_RMS_DBFS,
                     peak_db: float = DEFAULT_PEAK_DBFS, max_sec: float = WECHAT_VOICE_MAX_SEC,
                     trim: bool = True) -> Tuple[Playback, float]:
    """解码 → 裁首尾静音 → 重采样 → 响度归一，返回 ``(Playback, duration_sec)``。超长直接抛。

    时长按**裁剪后**算（送给 echo 步核对气泡秒数的就是它）。
    """
    if not info.present:
        raise RuntimeError(info.error or "cable_not_installed")
    data, sr = load_audio(path)
    raw_ms = int(len(data) * 1000 / float(sr or 1))
    if trim:
        data = trim_silence(data, sr)
    dur = len(data) / float(sr or 1)
    if dur <= 0.2:
        raise RuntimeError("audio_too_short")
    if dur > max_sec:
        raise RuntimeError(f"audio_too_long:{dur:.1f}s")
    data, gain_db = normalize_loudness(resample(data, sr, info.render_sr), info.render_sr,
                                       target_rms_db=target_rms_db, peak_db=peak_db)
    pb = Playback(data, info.render_sr, info.render_index, gain_db=gain_db,
                  trimmed_ms=max(0, raw_ms - int(dur * 1000)))
    return pb, dur


# ── 给 service 用的门面 ─────────────────────────────────────────────────────────
class VoiceCable:
    """驱动进程里的语音通路句柄：缓存设备发现、提供 mic 切换上下文与播放闭包。"""

    REFRESH_SEC = 60.0

    def __init__(self, state_dir: str = "", *, target_rms_db: float = DEFAULT_TARGET_RMS_DBFS,
                 peak_db: float = DEFAULT_PEAK_DBFS) -> None:
        self.state_path = os.path.join(state_dir or os.getcwd(), "mic_switch.json")
        self.target_rms_db = float(target_rms_db)
        self.peak_db = float(peak_db)
        self._info: Optional[CableInfo] = None
        self._checked = 0.0
        self.healed = heal_default_mic(self.state_path) if available() else None

    def info(self, refresh: bool = False) -> CableInfo:
        if refresh or self._info is None or time.monotonic() - self._checked > self.REFRESH_SEC:
            self._info = find_cable()
            self._checked = time.monotonic()
        return self._info

    def ready(self) -> bool:
        i = self.info()
        return bool(i.present and i.rates_match and (i.capture_endpoint_id or not _COM_OK))

    def status(self) -> Dict[str, Any]:
        return cable_status()

    def mic_switched(self) -> MicSwitch:
        return MicSwitch(self.info(), self.state_path)

    def mic_busy(self) -> Dict[str, Any]:
        """坐席的麦克风是否正被别的程序录着（排除我们自己的声卡端点与本进程）；见 :func:`capture_in_use`。"""
        cable = self.info().capture_endpoint_id
        return capture_in_use(exclude_endpoint_ids=(cable,) if cable else (), exclude_pids=(os.getpid(),))

    def prepare(self, path: str) -> Tuple[Playback, float]:
        return prepare_playback(path, self.info(), target_rms_db=self.target_rms_db, peak_db=self.peak_db)


__all__ = ["available", "CableInfo", "find_cable", "cable_status", "align_formats", "loopback_selftest",
           "default_capture_endpoint", "set_default_capture_endpoint", "endpoint_friendly_name",
           "list_capture_endpoints", "capture_in_use", "heal_default_mic", "MicSwitch", "load_audio", "resample",
           "normalize_peak",
           "trim_silence", "active_rms", "normalize_loudness", "Playback",
           "audio_duration_sec", "prepare_playback", "VoiceCable", "WECHAT_VOICE_MAX_SEC",
           "DEFAULT_PEAK_DBFS", "DEFAULT_TARGET_RMS_DBFS", "CABLE_RENDER_NAME", "CABLE_CAPTURE_NAME"]
