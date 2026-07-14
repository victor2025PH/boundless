#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试设备管理器修复
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.device_control.device_manager import get_device_manager

def main():
    config_path = "config/devices.yaml"
    manager = get_device_manager(config_path)
    
    print("测试设备管理器修复")
    print("=" * 60)
    
    # 发现设备
    discovered = manager.discover_devices()
    print(f"发现的设备: {discovered}")
    
    # 获取所有设备
    all_devices = manager.get_all_devices()
    print(f"\n所有设备 ({len(all_devices)}):")
    for d in all_devices:
        print(f"  - {d.display_name} ({d.device_id}): {d.status.value}")
    
    # 获取已连接设备
    connected_devices = manager.get_connected_devices()
    print(f"\n已连接设备 ({len(connected_devices)}):")
    for d in connected_devices:
        print(f"  - {d.display_name} ({d.device_id}): {d.status.value}")
        
        # 测试截图
        print(f"    测试截图...")
        screenshot = manager.capture_screen(d.device_id)
        if screenshot:
            print(f"    截图成功: {len(screenshot)} bytes")
        else:
            print(f"    截图失败")
            
        # 测试点击
        print(f"    测试点击...")
        success = manager.input_tap(d.device_id, 360, 800)
        print(f"    点击结果: {'成功' if success else '失败'}")
        
        # 测试返回键
        print(f"    测试返回键...")
        success = manager.input_keyevent(d.device_id, 4)
        print(f"    返回键结果: {'成功' if success else '失败'}")
    
    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)