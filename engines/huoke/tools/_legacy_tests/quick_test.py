#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
快速测试脚本
测试设备管理和基本功能
"""

import sys
import time
from pathlib import Path

# 添加项目根目录到Python路径
sys.path.insert(0, str(Path(__file__).parent))

from src.device_control.device_manager import get_device_manager


def main():
    print("快速测试设备管理功能")
    print("=" * 60)
    
    config_path = "config/devices.yaml"
    
    try:
        # 获取设备管理器
        manager = get_device_manager(config_path)
        
        # 发现设备
        print("发现设备...")
        discovered = manager.discover_devices()
        print(f"发现 {len(discovered)} 个设备: {discovered}")
        
        # 获取所有设备信息
        devices = manager.get_all_devices()
        print(f"\n总共 {len(devices)} 个设备:")
        
        for i, device in enumerate(devices, 1):
            print(f"{i}. {device.display_name} ({device.device_id})")
            print(f"   状态: {device.status.value}")
            print(f"   型号: {device.model}, Android {device.android_version}")
            
            if device.resolution:
                print(f"   分辨率: {device.resolution.get('width')}x{device.resolution.get('height')}")
        
        # 使用第一个实际设备
        real_devices = [d for d in devices if d.status.value == "connected" and d.device_id != "localhost"]
        if not real_devices:
            print("\n❌ 没有实际连接的设备")
            return False
        
        target_device = real_devices[0]
        device_id = target_device.device_id
        
        print(f"\n使用设备: {target_device.display_name} ({device_id})")
        
        # 测试截图
        print("\n测试截图功能...")
        timestamp = int(time.time())
        save_path = f"logs/test_screenshot_{timestamp}.png"
        
        Path("logs").mkdir(exist_ok=True)
        
        screenshot_data = manager.capture_screen(device_id, save_path)
        
        if screenshot_data:
            print(f"✅ 截图成功")
            print(f"   大小: {len(screenshot_data)} bytes")
            print(f"   保存到: {save_path}")
        else:
            print("❌ 截图失败")
        
        # 测试点击
        print("\n测试点击功能...")
        print("点击屏幕中心 (360, 800)...")
        success = manager.input_tap(device_id, 360, 800)
        print(f"点击结果: {'✅ 成功' if success else '❌ 失败'}")
        
        # 等待一下，避免连续操作
        time.sleep(1)
        
        # 测试返回键
        print("\n测试返回键...")
        success = manager.input_keyevent(device_id, 4)  # KEYCODE_BACK
        print(f"返回键结果: {'✅ 成功' if success else '❌ 失败'}")
        
        print("\n" + "=" * 60)
        print("快速测试完成!")
        
        return True
        
    except Exception as e:
        print(f"❌ 测试异常: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)