#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试Telegram启动
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.device_control.device_manager import get_device_manager

def main():
    print("测试Telegram启动")
    print("=" * 60)
    
    config_path = "config/devices.yaml"
    
    # 获取设备管理器
    manager = get_device_manager(config_path)
    
    # 获取已连接设备
    connected_devices = manager.get_connected_devices()
    if not connected_devices:
        print("❌ 没有已连接的设备")
        return False
    
    device = connected_devices[0]
    device_id = device.device_id
    print(f"使用设备: {device.display_name} ({device_id})")
    
    # 启动Telegram
    print("启动Telegram...")
    package = "org.telegram.messenger"
    activity = "org.telegram.ui.LaunchActivity"
    
    command = f"shell am start -n {package}/{activity}"
    success, output = manager.execute_adb_command(command, device_id)
    
    if success:
        print("✅ Telegram启动成功")
        print(f"输出: {output}")
        
        # 等待应用加载
        print("等待应用加载...")
        time.sleep(3)
        
        # 检查Telegram是否在运行
        command = f"shell ps | grep {package}"
        success, output = manager.execute_adb_command(command, device_id)
        
        if success and package in output:
            print("✅ Telegram进程正在运行")
        else:
            print("⚠️ Telegram进程可能未运行")
        
        # 获取当前Activity
        command = "shell dumpsys window windows | grep -E 'mCurrentFocus|mFocusedApp'"
        success, output = manager.execute_adb_command(command, device_id)
        
        if success and output:
            print(f"当前窗口信息:\n{output}")
            
            # 解析Activity
            for line in output.split('\n'):
                if 'ActivityRecord' in line:
                    parts = line.split(' ')
                    for part in parts:
                        if '/' in part and '.' in part:
                            print(f"当前Activity: {part.strip()}")
                            break
        
        # 截取屏幕查看状态
        print("截取屏幕查看状态...")
        timestamp = int(time.time())
        screenshot_path = f"logs/telegram_launch_{timestamp}.png"
        
        screenshot_data = manager.capture_screen(device_id, screenshot_path)
        if screenshot_data:
            print(f"✅ 截图成功: {screenshot_path}")
            print(f"大小: {len(screenshot_data)} bytes")
        else:
            print("❌ 截图失败")
            
        return True
        
    else:
        print("❌ Telegram启动失败")
        print(f"错误: {output}")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)