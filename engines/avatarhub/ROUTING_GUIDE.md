# Phase 3 – Routing Architecture Guide

## System Overview

```
Microphone ──► RVC v2 (conda: rvc) ──► Virtual Audio Cable ──► OBS Studio
                                                                    │
Webcam/NDI ──► FaceFusion (conda: facefusion) ──► Virtual Camera ──┘
                                                                    │
                                                               Stream / Record
```

---

## Part A – Audio Routing with Virtual Audio Cable (VAC)

### 1. Install Virtual Audio Cable
- Download from **VB-Audio**: https://vb-audio.com/Cable/index.htm
- Install `VBCABLE_Driver_Pack43.zip` → run `VBCABLE_Setup_x64.exe` as Administrator → Reboot.
- After reboot you will have two new audio devices:
  - `CABLE Input` (virtual speaker / sink)
  - `CABLE Output` (virtual microphone / source)

### 2. Configure RVC to Output to VAC
1. Open the RVC WebUI at `http://localhost:7865`.
2. Go to the **VC Inference** tab → expand **I/O Devices** (or similar, UI varies by version).
3. Set **Input Device** → your physical microphone.
4. Set **Output Device** → `CABLE Input (VB-Audio Virtual Cable)`.
5. Load your `.pth` model and `.index` file, then click **Start Voice Conversion**.

> RVC now sends converted audio **into** the CABLE Input, which makes it available as "CABLE Output" to any other application.

### 3. Configure OBS to Receive Converted Audio
1. In OBS → **Settings → Audio** → set one of the **Auxiliary Audio** slots to `CABLE Output (VB-Audio Virtual Cable)`.
2. Back in the main window, the CABLE Output will appear as an audio mixer track.
3. Optionally add a **Noise Suppression** or **VST 2.x Plug-in** filter on that track for further polish.
4. **Mute your original microphone** source in OBS so viewers only hear the RVC-converted voice.

---

## Part B – Video Routing via FaceFusion Virtual Camera

### 1. Install a Virtual Camera Driver
FaceFusion outputs frames via a virtual camera. The recommended driver is **OBS VirtualCam** (bundled with OBS ≥ 27) or **UnityCapture**.

If using OBS VirtualCam:
- OBS → **Tools → Virtual Camera → Start Virtual Camera**.

If FaceFusion exposes its own virtual device, it will appear as **"FaceFusion Virtual Camera"** in device lists automatically (requires [unitycapture](https://github.com/schellingb/UnityCapture) installed).

### 2. Route FaceFusion → OBS
Option A – **FaceFusion exposes a virtual camera directly**:
1. In OBS, add a **Video Capture Device** source.
2. Set Device → `FaceFusion Virtual Camera` (or the name shown by your virtual driver).
3. FaceFusion streams swapped frames in real-time to that device.

Option B – **Output via local stream (fallback)**:
1. In FaceFusion, enable the **Stream** output mode (if available) to push to `rtmp://127.0.0.1:1935/live`.
2. In OBS, add a **Media Source** with `rtmp://127.0.0.1:1935/live` as the network stream URL.

### 3. Recommended OBS Scene Layout
```
Scene: "AI Live"
├── [Video Capture Device]  → FaceFusion Virtual Camera   (full-screen)
└── [Audio Input Capture]   → CABLE Output (RVC voice)    (audio only)
```

---

## Part C – VRAM Budget Reference

| Process      | Approx. VRAM | Note                                      |
|--------------|-------------|-------------------------------------------|
| FaceFusion   | ≤ 8 GB      | Hard-capped via `--memory-limit 8`        |
| RVC v2       | 1 – 2 GB    | Soft-capped via `PYTORCH_CUDA_ALLOC_CONF` |
| OS / driver  | ~0.3 GB     | Always reserved                           |
| **Total**    | **≤ 10.3 GB** | **Leaves ~1.7 GB safety margin on 12 GB** |

If you experience VRAM pressure:
- Lower FaceFusion's `--memory-limit` to `6` in `start_facefusion.bat`.
- In RVC, use a smaller model (e.g., RVC 40k instead of 48k) to reduce VRAM footprint.

---

## Part D – System-Level Dependencies Checklist

| Dependency | Required By | Install Link |
|---|---|---|
| **Miniconda / Anaconda** | Both | https://docs.conda.io/en/latest/miniconda.html |
| **Git for Windows** | Both (git clone) | https://git-scm.com/download/win |
| **CUDA Toolkit 11.8** | Both (runtime) | https://developer.nvidia.com/cuda-11-8-0-download-archive |
| **cuDNN 8.x for CUDA 11.8** | Both | https://developer.nvidia.com/cudnn (free NVIDIA account required) |
| **FFmpeg (global PATH)** | RVC (required), FaceFusion (recommended) | https://www.gyan.dev/ffmpeg/builds/ → `ffmpeg-release-essentials.zip` |
| **Visual Studio Build Tools 2022** | FaceFusion install.py (compiles C extensions) | https://visualstudio.microsoft.com/visual-cpp-build-tools/ – select "Desktop development with C++" |
| **VB-Audio Virtual Cable** | OBS audio routing | https://vb-audio.com/Cable/index.htm |
| **OBS Studio ≥ 29** | Streaming/recording | https://obsproject.com/ |

> **Important**: Install CUDA Toolkit and cuDNN **before** running either install script. The PyTorch wheels embed their own CUDA runtime libraries, but ONNX Runtime CUDA (used by FaceFusion) requires the system CUDA libraries to be present.
