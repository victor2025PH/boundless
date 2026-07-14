# -*- coding: utf-8 -*-
"""
手机音频桥接模块 - 让手机麦克风成为PC音频输入源
用途：配合RVC实时变声，单手机完成音视频输入

方案A: DroidCam (推荐，已有实现)
  - 手机安装 DroidCam App
  - PC安装 DroidCam Client
  - 提供虚拟音频设备 "DroidCam Audio"

方案B: ADB+网络流 (备用)
  - 通过ADB录制手机音频并转发

使用说明：
1. 确保手机和PC在同一WiFi
2. 手机打开 DroidCam App，记录IP地址
3. PC端 DroidCam Client 连接手机
4. RVC选择 "DroidCam Audio" 作为输入设备
"""
import subprocess
import sys
import time
import socket
import threading
import numpy as np
import sounddevice as sd
from pathlib import Path

ADB = r"C:\platform-tools\adb.exe"
DEVICE_ID = None  # 自动检测


class PhoneAudioBridge:
    """手机音频桥接器 - 将手机麦克风音频转发到PC虚拟设备"""
    
    def __init__(self, samplerate=44100, block_size=1024):
        self.samplerate = samplerate
        self.block_size = block_size
        self.running = False
        self.stream = None
        self.audio_buffer = []
        self.lock = threading.Lock()
        
    def list_droidcam_devices(self):
        """列出可用的 DroidCam 音频设备"""
        devices = sd.query_devices()
        droidcam_devices = []
        for i, d in enumerate(devices):
            name = d.get('name', '')
            if 'droid' in name.lower() or 'droidcam' in name.lower():
                droidcam_devices.append({
                    'index': i,
                    'name': name,
                    'channels': d.get('max_input_channels', 0),
                    'samplerate': d.get('default_samplerate', 44100)
                })
        return droidcam_devices
    
    def list_all_input_devices(self):
        """列出所有音频输入设备"""
        devices = sd.query_devices()
        input_devices = []
        for i, d in enumerate(devices):
            if d.get('max_input_channels', 0) > 0:
                input_devices.append({
                    'index': i,
                    'name': d.get('name', ''),
                    'channels': d.get('max_input_channels', 0),
                    'samplerate': int(d.get('default_samplerate', 44100))
                })
        return input_devices
    
    def get_recommended_device(self):
        """获取推荐的手机音频输入设备 (DroidCam > 其他)"""
        # 优先找 DroidCam
        droid = self.list_droidcam_devices()
        if droid:
            # 选采样率匹配44100的
            for d in droid:
                if d['samplerate'] == 44100:
                    return d
            return droid[0]
        return None
    
    def start_capture(self, device_index=None, callback=None):
        """开始从手机捕获音频
        
        Args:
            device_index: 设备索引，None则自动选择DroidCam
            callback: 音频数据回调函数(data, frames, time, status)
        """
        if device_index is None:
            dev = self.get_recommended_device()
            if dev is None:
                raise RuntimeError("未找到手机音频设备，请确认DroidCam已连接")
            device_index = dev['index']
            print(f"[PhoneAudio] 自动选择设备: {dev['name']}")
        
        def default_callback(indata, frames, time_info, status):
            if status:
                print(f"[PhoneAudio] Status: {status}")
            with self.lock:
                self.audio_buffer.append(indata.copy())
            if callback:
                callback(indata, frames, time_info, status)
        
        self.stream = sd.InputStream(
            device=device_index,
            channels=1,
            samplerate=self.samplerate,
            blocksize=self.block_size,
            dtype=np.float32,
            callback=default_callback
        )
        self.stream.start()
        self.running = True
        print(f"[PhoneAudio] 开始捕获: device={device_index}, sr={self.samplerate}")
        return True
    
    def stop_capture(self):
        """停止捕获"""
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        self.running = False
        print("[PhoneAudio] 停止捕获")
    
    def get_audio_chunk(self, clear=True):
        """获取缓存的音频数据"""
        with self.lock:
            if not self.audio_buffer:
                return None
            # 合并所有缓冲区
            audio = np.concatenate(self.audio_buffer, axis=0)
            if clear:
                self.audio_buffer.clear()
            return audio
    
    def test_devices(self):
        """测试并显示可用设备"""
        print("\n" + "="*60)
        print("手机音频设备检测")
        print("="*60)
        
        # 所有输入设备
        all_inputs = self.list_all_input_devices()
        print(f"\n[所有输入设备] 共{len(all_inputs)}个:")
        for d in all_inputs:
            marker = ""
            if 'droid' in d['name'].lower():
                marker = " <-- [手机音频]"
            print(f"  [{d['index']}] {d['name']} ({d['channels']}ch, {d['samplerate']}Hz){marker}")
        
        # DroidCam专用列表
        droid = self.list_droidcam_devices()
        print(f"\n[DroidCam设备] 共{len(droid)}个:")
        if droid:
            for d in droid:
                print(f"  [{d['index']}] {d['name']} ({d['samplerate']}Hz)")
        else:
            print("  (未找到 - 请检查DroidCam是否已连接)")
        
        # 推荐设备
        rec = self.get_recommended_device()
        if rec:
            print(f"\n[推荐设备] [{rec['index']}] {rec['name']}")
            if rec['samplerate'] != 44100:
                print(f"  ⚠️ 警告: 采样率{rec['samplerate']}Hz，建议与输出设备匹配")
        else:
            print("\n[推荐设备] 无 (请连接DroidCam)")
        
        print("="*60 + "\n")
        return droid


class PhoneAudioRVCBridge:
    """手机音频到RVC的桥接 (配合api_240604.py使用)"""
    
    def __init__(self, rvc_api_url="http://127.0.0.1:6242"):
        self.rvc_url = rvc_api_url
        self.bridge = PhoneAudioBridge()
        self.converting = False
        
    def setup_droidcam_input(self):
        """配置DroidCam为输入源"""
        droid = self.bridge.get_recommended_device()
        if droid is None:
            print("[RVCBridge] 错误: 未找到DroidCam设备")
            print("[RVCBridge] 请执行以下步骤:")
            print("  1. 手机安装DroidCam App")
            print("  2. PC安装DroidCam Client")
            print("  3. 确保手机和PC在同一WiFi")
            print("  4. 手机打开DroidCam，记录IP")
            print("  5. PC端DroidCam Client连接手机")
            return False
        
        device_idx = droid['index']
        device_name = droid['name']
        
        # 设置sounddevice默认设备
        try:
            sd.default.device[0] = device_idx
            print(f"[RVCBridge] 已设置默认输入: [{device_idx}] {device_name}")
            
            # 检查采样率兼容性
            if droid['samplerate'] != 44100:
                print(f"[RVCBridge] ⚠️ 注意: DroidCam采样率{droid['samplerate']}Hz")
                print(f"[RVCBridge] 请确保RVC输出设备也使用相同采样率")
            return True
        except Exception as e:
            print(f"[RVCBridge] 设置失败: {e}")
            return False
    
    def get_config_for_rvc(self):
        """生成适合RVC API的配置数据"""
        droid = self.bridge.get_recommended_device()
        if droid is None:
            return None
        
        return {
            "sg_input_device": droid['name'],
            "sg_output_device": "CABLE Input (VB-Audio Virtual Cable)",
            "threhold": -60,
            "pitch": 0,
            "formant": 0.0,
            "index_rate": 0.3,
            "rms_mix_rate": 0.0,
            "block_time": 0.25,
            "crossfade_length": 0.05,
            "extra_time": 2.5,
            "n_cpu": 4,
            "I_noise_reduce": False,
            "O_noise_reduce": False,
            "use_pv": False,
            "f0method": "pm"  # pm模式更稳定
        }
    
    def print_setup_guide(self):
        """打印完整的设置指南"""
        guide = """
╔══════════════════════════════════════════════════════════════╗
║           手机音频输入 + RVC 实时变声 设置指南                  ║
╠══════════════════════════════════════════════════════════════╣

【一、手机端设置】
1. 安装 DroidCam App (Google Play / App Store)
2. 打开App，选择 "WiFi/LAN" 模式
3. 记录显示的IP地址 (如 192.168.1.100:4747)

【二、PC端设置】
1. 安装 DroidCam Client (https://www.dev47apps.com/)
2. 启动 DroidCam Client
3. 选择 "WiFi" 模式
4. 输入手机显示的IP地址，点击 Connect
5. 确认连接成功后，能看到手机摄像头画面

【三、音频设置】
1. 在DroidCam Client中勾选 "Audio" 选项
2. 系统会新增 "DroidCam Audio" 虚拟设备

【四、RVC变声配置】
1. 打开RVC实时变声 (gui_v1.py 或 api_240604.py)
2. 输入设备选择: "DroidCam Audio (MME)"
3. 输出设备选择: "CABLE Input (VB-Audio Virtual Cable) (MME)"
4. ⚠️ 重要: 输入输出都用MME模式，避免采样率不匹配

【五、测试】
1. 手机播放音乐或说话
2. PC上应能听到变声后的音频从VB-Cable输出

【常见问题】
Q: 提示 "Invalid sample rate"
A: 确保输入输出设备采样率一致 (44100Hz)，MME驱动更稳定

Q: 有回声或延迟
A: 调整RVC的block_time参数 (0.25 → 0.5)

Q: 音频断断续续
A: 检查WiFi信号，降低block_time到0.1

【设备选择参考】
"""
        print(guide)
        self.bridge.test_devices()


def test_phone_audio():
    """测试手机音频捕获"""
    bridge = PhoneAudioBridge()
    bridge.test_devices()
    
    # 尝试捕获5秒
    droid = bridge.get_recommended_device()
    if droid:
        print("\n[测试] 开始5秒音频捕获...")
        bridge.start_capture()
        time.sleep(5)
        bridge.stop_capture()
        
        audio = bridge.get_audio_chunk()
        if audio is not None:
            print(f"[测试] 捕获音频: {len(audio)} samples, shape={audio.shape}")
            print("[测试] 手机音频工作正常 ✓")
        else:
            print("[测试] 未捕获到音频 ✗")
    else:
        print("\n[测试] 跳过捕获 (无设备)")


def setup_for_rvc():
    """一键配置RVC使用手机音频"""
    rvc_bridge = PhoneAudioRVCBridge()
    rvc_bridge.print_setup_guide()
    
    if rvc_bridge.setup_droidcam_input():
        config = rvc_bridge.get_config_for_rvc()
        print("\n[一键配置] 生成的RVC配置:")
        for k, v in config.items():
            print(f"  {k}: {v}")
        return config
    return None


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="手机音频桥接工具")
    parser.add_argument("command", choices=["test", "setup", "devices"], 
                       help="test=测试捕获, setup=生成RVC配置, devices=列出设备")
    args = parser.parse_args()
    
    if args.command == "test":
        test_phone_audio()
    elif args.command == "setup":
        setup_for_rvc()
    elif args.command == "devices":
        bridge = PhoneAudioBridge()
        bridge.test_devices()
