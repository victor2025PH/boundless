#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram自动化测试脚本
测试完整的搜索用户和发送消息流程
"""

import sys
import time
import logging
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from src.device_control.device_manager import get_device_manager
from src.app_automation.telegram import create_telegram_automation


def setup_logging():
    """设置日志配置"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler('logs/telegram_test.log')
        ]
    )


def test_device_discovery():
    """测试设备发现"""
    print("\n=== 测试设备发现 ===")
    
    config_path = project_root / "config" / "devices.yaml"
    manager = get_device_manager(str(config_path))
    
    devices = manager.get_all_devices()
    
    if not devices:
        print("❌ 没有发现设备")
        return False
    
    print(f"✅ 发现 {len(devices)} 个设备:")
    for i, device in enumerate(devices, 1):
        print(f"  {i}. {device.display_name} ({device.device_id})")
        print(f"     状态: {device.status.value}")
        print(f"     型号: {device.model}, Android {device.android_version}")
        print(f"     分辨率: {device.resolution.get('width', 0)}x{device.resolution.get('height', 0)}")
    
    return True


def test_telegram_start(telegram_auto, device_id):
    """测试Telegram启动"""
    print("\n=== 测试Telegram启动 ===")
    
    try:
        print(f"在设备 {device_id} 上启动Telegram...")
        success = telegram_auto.start_telegram(device_id)
        
        if success:
            print("✅ Telegram启动成功")
        else:
            print("❌ Telegram启动失败")
            
        return success
        
    except Exception as e:
        print(f"❌ Telegram启动异常: {e}")
        return False


def test_search_user(telegram_auto, device_id, username):
    """测试搜索用户"""
    print(f"\n=== 测试搜索用户 @{username} ===")
    
    try:
        print(f"搜索用户 @{username}...")
        success = telegram_auto.search_and_open_user(username, device_id)
        
        if success:
            print(f"✅ 用户 @{username} 搜索成功")
        else:
            print(f"❌ 用户 @{username} 搜索失败")
            
        return success
        
    except Exception as e:
        print(f"❌ 搜索用户异常: {e}")
        return False


def test_send_message(telegram_auto, device_id, message):
    """测试发送消息"""
    print(f"\n=== 测试发送消息 ===")
    
    try:
        print(f"发送消息: {message[:50]}...")
        success = telegram_auto.send_text_message(message, device_id)
        
        if success:
            print("✅ 消息发送成功")
        else:
            print("❌ 消息发送失败")
            
        return success
        
    except Exception as e:
        print(f"❌ 发送消息异常: {e}")
        return False


def test_screenshot(telegram_auto, device_id):
    """测试截图发送"""
    print("\n=== 测试截图发送 ===")
    
    try:
        # 先截图保存
        timestamp = int(time.time())
        screenshot_path = f"logs/screenshots/test_{timestamp}.png"
        
        print(f"截取屏幕并保存到: {screenshot_path}")
        
        # 使用设备管理器截图
        manager = get_device_manager(str(project_root / "config" / "devices.yaml"))
        screenshot_data = manager.capture_screen(device_id, screenshot_path)
        
        if screenshot_data:
            print(f"✅ 截图成功，大小: {len(screenshot_data)} bytes")
            print(f"   文件已保存: {screenshot_path}")
            
            # 测试发送截图
            print("测试发送截图到Telegram...")
            success = telegram_auto.send_screenshot(device_id, screenshot_path)
            
            if success:
                print("✅ 截图发送成功")
            else:
                print("❌ 截图发送失败")
                
            return success
        else:
            print("❌ 截图失败")
            return False
            
    except Exception as e:
        print(f"❌ 截图测试异常: {e}")
        return False


def test_complete_workflow(telegram_auto, device_id, username, message):
    """测试完整工作流程"""
    print(f"\n=== 测试完整工作流程 ===")
    print(f"目标: 搜索 @{username} → 发送消息 → 发送截图")
    
    try:
        success = telegram_auto.complete_workflow(username, message, include_screenshot=True)
        
        if success:
            print("✅ 完整工作流程执行成功")
        else:
            print("❌ 完整工作流程执行失败")
            
        return success
        
    except Exception as e:
        print(f"❌ 完整工作流程异常: {e}")
        return False


def main():
    """主测试函数"""
    setup_logging()
    
    print("=" * 80)
    print("Telegram自动化测试脚本")
    print("=" * 80)
    
    # 确保日志目录存在
    Path("logs").mkdir(exist_ok=True)
    Path("logs/screenshots").mkdir(exist_ok=True)
    
    # 测试配置
    config_path = str(project_root / "config" / "devices.yaml")
    username = "aixinwww"
    test_message = "这是来自手机自动化测试的消息。时间: " + time.strftime("%Y-%m-%d %H:%M:%S")
    
    # 1. 测试设备发现
    if not test_device_discovery():
        print("设备发现测试失败，退出")
        return False
    
    # 获取设备管理器
    manager = get_device_manager(config_path)
    devices = manager.get_all_devices()
    
    # 使用第一个实际连接的设备
    real_devices = [d for d in devices if d.status.value == "connected" and d.device_id != "localhost"]
    if not real_devices:
        print("没有实际连接的设备")
        return False
    
    device_id = real_devices[0].device_id
    device_name = real_devices[0].display_name
    print(f"\n使用设备: {device_name} ({device_id})")
    
    # 创建Telegram自动化实例
    try:
        telegram_auto = create_telegram_automation(config_path)
        telegram_auto.set_current_device(device_id)
    except Exception as e:
        print(f"创建Telegram自动化实例失败: {e}")
        return False
    
    # 2. 测试Telegram启动
    if not test_telegram_start(telegram_auto, device_id):
        print("Telegram启动测试失败，继续其他测试")
    
    # 3. 测试搜索用户
    if not test_search_user(telegram_auto, device_id, username):
        print("搜索用户测试失败")
        # 不返回，继续测试
    
    # 4. 测试发送消息
    if not test_send_message(telegram_auto, device_id, test_message):
        print("发送消息测试失败")
    
    # 5. 测试截图
    if not test_screenshot(telegram_auto, device_id):
        print("截图测试失败")
    
    # 6. 测试完整工作流程（如果上述测试都通过）
    print("\n" + "=" * 80)
    print("总结测试结果")
    print("=" * 80)
    
    # 询问用户是否要测试完整工作流程
    print("\n是否要测试完整工作流程？")
    print("这将执行: 搜索用户 → 发送消息 → 发送截图")
    print("注意: 这将在Telegram中实际发送消息")
    
    response = input("输入 'yes' 继续，其他跳过: ").strip().lower()
    
    if response == 'yes':
        print("开始完整工作流程测试...")
        test_complete_workflow(telegram_auto, device_id, username, test_message)
    else:
        print("跳过完整工作流程测试")
    
    print("\n测试完成!")
    print("请检查Telegram应用确认消息是否成功发送")
    
    return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)