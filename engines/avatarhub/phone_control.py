"""
手机控制 + AI API 联调测试脚本
设备：红米13C (7X8LJRIRHA4LT8VG)
用法：python phone_control.py [test|screenshot|swipe|tap|faceswap|tts|audio]

新增：音频输入桥接 (P23-1)
  - audio     : 测试手机麦克风作为PC音频输入
  - audio_setup: 配置DroidCam音频+RVC变声
"""
import subprocess
import sys
import os
import base64
import time
import requests
import threading
import numpy as np
import sounddevice as sd
from pathlib import Path
import app_config
_B = str(app_config.BASE)

ADB = r"C:\platform-tools\adb.exe"
DEVICE_ID = "JZBIGUKZS4NBAYDI"  # USB ADB
SERVER = "http://127.0.0.1"
FACESWAP_URL = f"{SERVER}:8000/faceswap"
TTS_URL = f"{SERVER}:7851/v1/audio/speech"
SCREEN_W, SCREEN_H = 720, 1600


# ── ADB 基础封装 ─────────────────────────────────────────────────

def adb(*args, capture=True):
    cmd = [ADB, "-s", DEVICE_ID] + list(args)
    if capture:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return r.stdout.strip()
    else:
        subprocess.run(cmd, timeout=30)


def screenshot(save_path=f"{_B}/phone_screen.png"):
    """截图并保存到本地"""
    cmd = [ADB, "-s", DEVICE_ID, "exec-out", "screencap", "-p"]
    result = subprocess.run(cmd, capture_output=True, timeout=15)
    with open(save_path, "wb") as f:
        f.write(result.stdout)
    size = Path(save_path).stat().st_size
    print(f"[截图] 已保存: {save_path} ({size//1024} KB)")
    return save_path


def tap(x, y):
    """点击屏幕坐标"""
    adb("shell", "input", "tap", str(x), str(y))
    print(f"[点击] ({x}, {y})")


def swipe(x1, y1, x2, y2, duration_ms=300):
    """滑动"""
    adb("shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms))
    print(f"[滑动] ({x1},{y1}) → ({x2},{y2})")


def input_text(text):
    """输入文字（英文/数字）"""
    adb("shell", "input", "text", text)
    print(f"[输入] {text}")


def press_key(keycode):
    """按键，keycode 参考 Android KeyEvent"""
    adb("shell", "input", "keyevent", str(keycode))


def wake_screen():
    """唤醒屏幕"""
    adb("shell", "input", "keyevent", "224")
    time.sleep(0.5)


def unlock_swipe():
    """上滑解锁"""
    swipe(360, 1400, 360, 800, 400)
    time.sleep(0.5)


def get_screen_text():
    """获取当前屏幕 UI 树文本内容（用于识别界面状态）"""
    out = adb("shell", "uiautomator", "dump", "/sdcard/ui.xml")
    xml = adb("shell", "cat", "/sdcard/ui.xml")
    return xml


def launch_app(package):
    """启动应用"""
    adb("shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
    print(f"[启动] {package}")


def get_camera_photo(save_path=f"{_B}/phone_camera.jpg"):
    """拍一张照片并拉取到本机（需要手机已打开相机）"""
    remote = "/sdcard/DCIM/Camera/test_capture.jpg"
    adb("shell", "am", "start", "-a", "android.media.action.IMAGE_CAPTURE")
    time.sleep(2)
    press_key(27)
    time.sleep(2)
    adb("pull", remote, save_path)
    print(f"[拍照] 已保存: {save_path}")
    return save_path


# ── AI API 封装 ──────────────────────────────────────────────────

def api_faceswap(source_path, target_path, output_path=f"{_B}/faceswap_result.png"):
    """调用换脸 API"""
    with open(source_path, "rb") as f:
        src_b64 = base64.b64encode(f.read()).decode()
    with open(target_path, "rb") as f:
        tgt_b64 = base64.b64encode(f.read()).decode()

    print(f"[换脸] 调用 API...")
    t0 = time.time()
    resp = requests.post(FACESWAP_URL,
        json={"source_image": src_b64, "target_image": tgt_b64},
        timeout=120)
    resp.raise_for_status()
    result = resp.json()
    elapsed = result.get("elapsed_ms", 0)

    with open(output_path, "wb") as f:
        f.write(base64.b64decode(result["result_image"]))
    print(f"[换脸] 完成！耗时 {elapsed}ms，结果: {output_path}")
    return output_path


def api_tts(text, voice="female_01", language="zh-cn",
            output_path=f"{_B}/tts_result.wav"):
    """调用 TTS API 合成语音"""
    print(f"[TTS] 合成: {text[:30]}...")
    t0 = time.time()
    resp = requests.post(TTS_URL,
        json={"model": "xtts_v2", "input": text,
              "voice": voice, "language": language},
        timeout=60)
    resp.raise_for_status()
    with open(output_path, "wb") as f:
        f.write(resp.content)
    elapsed = round(time.time() - t0, 1)
    print(f"[TTS] 完成！耗时 {elapsed}s，音频: {output_path} ({len(resp.content)//1024} KB)")
    return output_path


def push_and_play_audio(wav_path):
    """把 WAV 推送到手机并播放"""
    remote = "/sdcard/tts_play.wav"
    adb("push", wav_path, remote)
    adb("shell", "am", "start", "-a", "android.intent.action.VIEW",
        "-d", f"file://{remote}", "-t", "audio/wav")
    print(f"[播放] 已推送并播放: {remote}")


def push_image_to_phone(local_path, remote_path="/sdcard/Pictures/faceswap_result.png"):
    """把图片推送到手机相册"""
    adb("push", local_path, remote_path)
    adb("shell", "am", "broadcast", "-a",
        "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
        "-d", f"file://{remote_path}")
    print(f"[推送] 图片已推送到手机: {remote_path}")


# ── 测试函数 ─────────────────────────────────────────────────────

def test_adb():
    print("\n=== ADB 连接测试 ===")
    model = adb("shell", "getprop", "ro.product.model")
    android = adb("shell", "getprop", "ro.build.version.release")
    battery = adb("shell", "dumpsys", "battery")
    level = [l for l in battery.splitlines() if "level" in l]
    print(f"设备: {model}  Android: {android}")
    print(f"电量: {level[0].strip() if level else '?'}")
    print("[OK] ADB 连接正常")


def test_screenshot():
    print("\n=== 截图测试 ===")
    wake_screen()
    path = screenshot()
    print(f"[OK] 截图成功: {path}")
    return path


def test_faceswap_with_phone():
    print("\n=== 换脸测试（手机截图作为目标） ===")
    wake_screen()
    time.sleep(1)
    screen_path = screenshot(f"{_B}/phone_target.png")
    source_path = rf"{_B}\facefusion\.github\preview.png"
    if not Path(source_path).exists():
        print(f"[ERROR] 源人脸图不存在: {source_path}")
        return

    result_path = api_faceswap(source_path, screen_path,
                               f"{_B}/phone_faceswap_result.png")
    push_image_to_phone(result_path)
    print("[OK] 换脸结果已推送到手机相册")


def test_tts_on_phone():
    print("\n=== TTS + 手机播放测试 ===")
    wav = api_tts("你好，红米手机，语音合成测试成功！")
    push_and_play_audio(wav)
    print("[OK] TTS 音频已推送到手机播放")


# ═════════════════════════════════════════════════════════════════
# P23-1: 手机音频输入桥接 (新增)
# ═════════════════════════════════════════════════════════════════

class PhoneAudioBridge:
    """手机音频输入桥接 - 让手机麦克风成为PC音频源"""
    
    def __init__(self, samplerate=44100, block_size=1024):
        self.samplerate = samplerate
        self.block_size = block_size
        self.running = False
        self.stream = None
        self.audio_buffer = []
        self.lock = threading.Lock()
        
    def list_droidcam_devices(self):
        """列出DroidCam音频设备"""
        devices = sd.query_devices()
        droidcam_devices = []
        for i, d in enumerate(devices):
            name = d.get('name', '')
            if 'droid' in name.lower():
                droidcam_devices.append({
                    'index': i,
                    'name': name,
                    'channels': d.get('max_input_channels', 0),
                    'samplerate': d.get('default_samplerate', 44100)
                })
        return droidcam_devices
    
    def get_recommended_device(self):
        """获取推荐的手机音频设备"""
        droid = self.list_droidcam_devices()
        if droid:
            for d in droid:
                if d['samplerate'] == 44100:
                    return d
            return droid[0]
        return None
    
    def start_capture(self, device_index=None, duration=5):
        """开始捕获手机音频"""
        if device_index is None:
            dev = self.get_recommended_device()
            if dev is None:
                print("[PhoneAudio] 错误: 未找到DroidCam设备")
                print("[PhoneAudio] 请确保:")
                print("  1. 手机安装了DroidCam App")
                print("  2. PC端DroidCam Client已连接手机")
                return False
            device_index = dev['index']
            print(f"[PhoneAudio] 使用设备: {dev['name']}")
        
        def audio_callback(indata, frames, time_info, status):
            with self.lock:
                self.audio_buffer.append(indata.copy())
        
        self.stream = sd.InputStream(
            device=device_index,
            channels=1,
            samplerate=self.samplerate,
            blocksize=self.block_size,
            dtype=np.float32,
            callback=audio_callback
        )
        self.stream.start()
        self.running = True
        print(f"[PhoneAudio] 开始捕获 ({duration}秒)...")
        time.sleep(duration)
        self.stop_capture()
        return True
    
    def stop_capture(self):
        """停止捕获"""
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        self.running = False
    
    def test_devices(self):
        """测试并显示可用设备"""
        print("\n" + "="*60)
        print("手机音频设备检测 (DroidCam)")
        print("="*60)
        
        all_inputs = [d for d in sd.query_devices() if d.get('max_input_channels', 0) > 0]
        print(f"\n[所有输入设备] 共{len(all_inputs)}个:")
        for i, d in enumerate(all_inputs):
            marker = " <-- [手机音频]" if 'droid' in d['name'].lower() else ""
            print(f"  [{i}] {d['name']}{marker}")
        
        droid = self.list_droidcam_devices()
        print(f"\n[DroidCam设备] 共{len(droid)}个:")
        if droid:
            for d in droid:
                print(f"  [{d['index']}] {d['name']} ({d['samplerate']}Hz)")
        else:
            print("  (未找到 - 请检查DroidCam是否已连接)")
        print("="*60 + "\n")
        return droid


def test_phone_audio():
    """测试手机音频输入"""
    print("\n=== 手机音频输入测试 ===")
    bridge = PhoneAudioBridge()
    droid = bridge.test_devices()
    
    if droid:
        if bridge.start_capture(duration=5):
            audio = np.concatenate(bridge.audio_buffer, axis=0) if bridge.audio_buffer else None
            if audio is not None:
                print(f"[OK] 捕获成功: {len(audio)} samples")
                # 简单音频分析
                rms = np.sqrt(np.mean(audio**2))
                print(f"[OK] 音频电平: {rms:.4f} (>{0.01}为正常)")
                if rms > 0.01:
                    print("[OK] 手机麦克风工作正常！✓")
                else:
                    print("[!] 音频电平较低，请检查手机麦克风")
            else:
                print("[FAIL] 未捕获到音频")
    else:
        print("[SKIP] 未找到DroidCam设备，跳过捕获测试")
        print("[HINT] 请按以下步骤设置:")
        print("  1. 手机安装DroidCam App")
        print("  2. PC安装DroidCam Client")
        print("  3. 手机打开DroidCam，记录WiFi IP")
        print("  4. PC端DroidCam Client连接该IP")


def setup_rvc_phone_audio():
    """配置RVC使用手机音频输入"""
    print("\n=== RVC + 手机音频 配置向导 ===\n")
    
    bridge = PhoneAudioBridge()
    droid = bridge.test_devices()
    
    if not droid:
        print("[!] 未找到DroidCam设备，无法配置")
        return False
    
    dev = bridge.get_recommended_device()
    print("\n[配置建议]")
    print(f"  输入设备: {dev['name']}")
    print(f"  输出设备: CABLE Input (VB-Audio Virtual Cable) (MME)")
    print(f"  采样率:   44100Hz (输入输出必须一致)")
    print(f"  变调:     根据角色调整 (建议±12)")
    print(f"  F0方法:   pm (最稳定)")
    print("\n[使用说明]")
    print("  1. 启动RVC实时变声 (gui_v1.py 或 api_240604.py)")
    print("  2. 输入设备选择上述设备名称")
    print("  3. 输出设备选择VB-Cable")
    print("  4. 点击Start开始实时变声")
    print("  5. 手机说话，PC端应能听到变声后音频")
    
    # 可选: 直接设置默认设备
    try:
        sd.default.device[0] = dev['index']
        print(f"\n[OK] 已设置系统默认输入设备为DroidCam")
    except Exception as e:
        print(f"[!] 设置默认设备失败: {e}")
    
    return True


def full_test():
    print("=" * 50)
    print(" 全流程测试：ADB + 换脸API + TTS + 手机控制 + 音频输入")
    print("=" * 50)
    test_adb()
    test_screenshot()
    test_faceswap_with_phone()
    test_tts_on_phone()
    test_phone_audio()  # P23-1: 新增音频输入测试
    print("\n[全部完成] 所有测试通过！")


# ── 入口 ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "test"

    cmds = {
        "test":        test_adb,
        "screenshot":  test_screenshot,
        "faceswap":    test_faceswap_with_phone,
        "tts":         test_tts_on_phone,
        "audio":       test_phone_audio,       # P23-1: 测试手机音频输入
        "audio_setup": setup_rvc_phone_audio,  # P23-1: 配置RVC手机音频
        "full":        full_test,
        "wake":        wake_screen,
        "unlock":      unlock_swipe,
    }

    if cmd not in cmds:
        print(f"用法: python phone_control.py [{'/'.join(cmds.keys())}]")
        sys.exit(1)

    cmds[cmd]()
