#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram自动化演示脚本
安全测试完整工作流程，包含用户确认步骤
"""

import sys
import time
import logging
from pathlib import Path

# 添加项目根目录到Python路径
sys.path.insert(0, str(Path(__file__).parent))

from src.device_control.device_manager import get_device_manager
from src.app_automation.telegram import create_telegram_automation


class TelegramDemo:
    """Telegram演示类"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.setup_logging()
        
        self.config_path = str(Path(__file__).parent / "config" / "devices.yaml")
        self.username = "aixinwww"
        self.test_message = "手机自动化测试消息 - " + time.strftime("%Y-%m-%d %H:%M:%S")
        
        # 确保日志目录存在
        Path("logs").mkdir(exist_ok=True)
        Path("logs/screenshots").mkdir(exist_ok=True)
        Path("logs/demo").mkdir(exist_ok=True)
    
    def setup_logging(self):
        """设置日志配置"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler(sys.stdout),
                logging.FileHandler('logs/demo_telegram.log')
            ]
        )
    
    def print_header(self, title):
        """打印标题"""
        print("\n" + "=" * 80)
        print(f" {title}")
        print("=" * 80)
    
    def print_step(self, step_num, description):
        """打印步骤"""
        print(f"\n[{step_num}] {description}")
        print("-" * 40)
    
    def get_user_confirmation(self, prompt):
        """获取用户确认"""
        print(f"\n{prompt}")
        response = input("请输入 'yes' 继续，或 'no' 跳过: ").strip().lower()
        return response == 'yes'
    
    def test_device_connection(self):
        """测试设备连接"""
        self.print_step("1", "测试设备连接")
        
        try:
            manager = get_device_manager(self.config_path)
            
            # 发现设备
            discovered = manager.discover_devices()
            if not discovered:
                print("❌ 没有发现设备")
                return None
            
            print(f"✅ 发现 {len(discovered)} 个设备: {discovered}")
            
            # 获取已连接设备
            connected_devices = manager.get_connected_devices()
            if not connected_devices:
                print("❌ 没有已连接的设备")
                return None
            
            device = connected_devices[0]
            print(f"✅ 使用设备: {device.display_name} ({device.device_id})")
            print(f"   型号: {device.model}, Android {device.android_version}")
            
            # 测试基础功能
            print("测试基础功能...")
            
            # 截图测试
            screenshot_data = manager.capture_screen(device.device_id)
            if screenshot_data:
                print(f"✅ 截图测试成功: {len(screenshot_data)} bytes")
            else:
                print("⚠️ 截图测试失败，但继续...")
            
            return device.device_id
            
        except Exception as e:
            print(f"❌ 设备连接测试失败: {e}")
            return None
    
    def test_telegram_start(self, telegram_auto, device_id):
        """测试Telegram启动"""
        self.print_step("2", "测试Telegram启动")
        
        if not self.get_user_confirmation("将在设备上启动Telegram应用。确定继续吗？"):
            print("跳过Telegram启动测试")
            return False
        
        try:
            print("启动Telegram...")
            success = telegram_auto.start_telegram(device_id)
            
            if success:
                print("✅ Telegram启动成功")
                print("请查看手机屏幕确认Telegram已启动")
                
                # 等待用户确认
                print("\n请查看手机:")
                print("1. Telegram是否已启动？")
                print("2. 是否显示主界面？")
                
                if self.get_user_confirmation("Telegram是否正常启动？"):
                    return True
                else:
                    print("⚠️ 用户报告Telegram启动异常")
                    return False
            else:
                print("❌ Telegram启动失败")
                return False
                
        except Exception as e:
            print(f"❌ Telegram启动测试异常: {e}")
            return False
    
    def test_search_user(self, telegram_auto, device_id):
        """测试搜索用户"""
        self.print_step("3", "测试搜索用户")
        
        print(f"将搜索用户: @{self.username}")
        print("这将在Telegram中执行搜索操作")
        
        if not self.get_user_confirmation("确定要搜索用户吗？"):
            print("跳过搜索用户测试")
            return False
        
        try:
            print(f"搜索 @{self.username}...")
            success = telegram_auto.search_and_open_user(self.username, device_id)
            
            if success:
                print("✅ 用户搜索成功")
                print("请查看手机屏幕确认:")
                print("1. 是否打开了搜索界面？")
                print("2. 是否显示了搜索结果？")
                print("3. 是否打开了聊天界面？")
                
                if self.get_user_confirmation("用户搜索是否成功？"):
                    return True
                else:
                    print("⚠️ 用户报告搜索异常")
                    return False
            else:
                print("❌ 用户搜索失败")
                return False
                
        except Exception as e:
            print(f"❌ 用户搜索测试异常: {e}")
            return False
    
    def test_send_message(self, telegram_auto, device_id, dry_run=True):
        """测试发送消息"""
        self.print_step("4", "测试发送消息")
        
        print(f"消息内容: {self.test_message}")
        
        if dry_run:
            print("⚠️ 当前为干跑模式，不会实际发送消息")
            print("如需实际发送，请修改dry_run=False")
            
            # 模拟发送
            print("模拟发送消息...")
            print("✅ 干跑模式完成")
            return True
        
        print("⚠️ 这将实际发送消息到Telegram")
        print(f"接收者: @{self.username}")
        
        if not self.get_user_confirmation("确定要发送消息吗？"):
            print("跳过消息发送测试")
            return False
        
        try:
            print("发送消息...")
            success = telegram_auto.send_text_message(self.test_message, device_id)
            
            if success:
                print("✅ 消息发送成功")
                print("请查看Telegram确认消息是否收到")
                
                if self.get_user_confirmation("消息是否成功发送？"):
                    return True
                else:
                    print("⚠️ 用户报告消息发送异常")
                    return False
            else:
                print("❌ 消息发送失败")
                return False
                
        except Exception as e:
            print(f"❌ 消息发送测试异常: {e}")
            return False
    
    def test_screenshot(self, telegram_auto, device_id, dry_run=True):
        """测试截图发送"""
        self.print_step("5", "测试截图发送")
        
        if dry_run:
            print("⚠️ 当前为干跑模式，不会实际发送截图")
            print("将仅截图保存，不发送")
        
        # 截图保存
        timestamp = int(time.time())
        screenshot_path = f"logs/screenshots/demo_{timestamp}.png"
        
        print(f"截取屏幕并保存到: {screenshot_path}")
        
        try:
            manager = get_device_manager(self.config_path)
            screenshot_data = manager.capture_screen(device_id, screenshot_path)
            
            if screenshot_data:
                print(f"✅ 截图成功: {len(screenshot_data)} bytes")
                print(f"   文件已保存: {screenshot_path}")
                
                if dry_run:
                    print("干跑模式，跳过截图发送")
                    return True
                
                print("⚠️ 这将实际发送截图到Telegram")
                
                if not self.get_user_confirmation("确定要发送截图吗？"):
                    print("跳过截图发送")
                    return True
                
                print("发送截图...")
                success = telegram_auto.send_screenshot(device_id, screenshot_path)
                
                if success:
                    print("✅ 截图发送成功")
                    print("请查看Telegram确认截图是否收到")
                    
                    if self.get_user_confirmation("截图是否成功发送？"):
                        return True
                    else:
                        print("⚠️ 用户报告截图发送异常")
                        return False
                else:
                    print("❌ 截图发送失败")
                    return False
            else:
                print("❌ 截图失败")
                return False
                
        except Exception as e:
            print(f"❌ 截图测试异常: {e}")
            return False
    
    def run_demo(self, dry_run=True):
        """运行演示"""
        self.print_header("Telegram自动化演示")
        
        print("演示配置:")
        print(f"  设备配置文件: {self.config_path}")
        print(f"  目标用户: @{self.username}")
        print(f"  运行模式: {'干跑模式（不实际发送）' if dry_run else '实际发送模式'}")
        print(f"  开始时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        
        print("\n注意事项:")
        print("1. 请确保手机已通过USB连接")
        print("2. 请确保已开启USB调试")
        print("3. 请确保Telegram已安装")
        print("4. 请保持手机屏幕解锁")
        
        if not self.get_user_confirmation("是否开始演示？"):
            print("演示取消")
            return False
        
        # 1. 测试设备连接
        device_id = self.test_device_connection()
        if not device_id:
            print("❌ 设备连接测试失败，演示终止")
            return False
        
        # 创建Telegram自动化实例
        try:
            telegram_auto = create_telegram_automation(self.config_path)
            telegram_auto.set_current_device(device_id)
        except Exception as e:
            print(f"❌ 创建Telegram自动化实例失败: {e}")
            return False
        
        # 2. 测试Telegram启动
        if not self.test_telegram_start(telegram_auto, device_id):
            print("⚠️ Telegram启动测试失败，但继续演示")
        
        # 3. 测试搜索用户
        if not self.test_search_user(telegram_auto, device_id):
            print("⚠️ 搜索用户测试失败，但继续演示")
        
        # 4. 测试发送消息
        if not self.test_send_message(telegram_auto, device_id, dry_run):
            print("⚠️ 发送消息测试失败")
        
        # 5. 测试截图
        if not self.test_screenshot(telegram_auto, device_id, dry_run):
            print("⚠️ 截图测试失败")
        
        # 演示完成
        self.print_header("演示完成")
        
        print("🎉 Telegram自动化演示已完成")
        print(f"完成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        
        if dry_run:
            print("\n干跑模式总结:")
            print("✅ 所有测试在干跑模式下完成")
            print("📁 截图已保存到 logs/screenshots/")
            print("📋 日志已保存到 logs/demo_telegram.log")
            print("\n下一步: 运行实际发送测试")
            print("  修改 dry_run=False 并重新运行")
        else:
            print("\n实际发送模式总结:")
            print("✅ 消息和截图已实际发送")
            print("📱 请检查Telegram确认接收情况")
            print("📋 详细日志: logs/demo_telegram.log")
        
        return True


def main():
    """主函数"""
    demo = TelegramDemo()
    
    # 默认为干跑模式，避免意外发送
    dry_run = True
    
    # 检查命令行参数
    if len(sys.argv) > 1 and sys.argv[1] == "--real":
        print("⚠️ 警告: 使用实际发送模式")
        print("这将实际发送消息和截图到Telegram")
        
        confirm = input("确定要使用实际发送模式吗？(输入 'yes' 确认): ").strip().lower()
        if confirm == 'yes':
            dry_run = False
        else:
            print("使用干跑模式")
    
    success = demo.run_demo(dry_run)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()