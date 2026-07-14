#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
手机自动化主程序
协调各个模块，提供命令行接口
"""

import argparse
import sys
import logging
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.device_control.device_manager import get_device_manager
from src.utils.log_config import setup_logging


def list_devices():
    """列出所有设备"""
    logger = logging.getLogger(__name__)
    logger.info("正在列出设备...")
    
    try:
        config_path = project_root / "config" / "devices.yaml"
        manager = get_device_manager(str(config_path))
        
        devices = manager.get_all_devices()
        
        if not devices:
            print("未找到任何设备")
            return False
        
        print(f"\n找到 {len(devices)} 个设备:")
        print("=" * 80)
        
        for i, device in enumerate(devices, 1):
            print(f"{i}. {device.display_name} ({device.device_id})")
            print(f"   状态: {device.status.value}")
            print(f"   型号: {device.model} | 制造商: {device.manufacturer}")
            print(f"   Android版本: {device.android_version}")
            if device.resolution:
                print(f"   分辨率: {device.resolution.get('width', 0)}x{device.resolution.get('height', 0)}")
            print(f"   DPI: {device.dpi}")
            print()
        
        return True
        
    except Exception as e:
        logger.error(f"列出设备失败: {e}")
        return False


def capture_screen(device_id=None, save_path=None):
    """截取屏幕"""
    logger = logging.getLogger(__name__)
    
    try:
        config_path = project_root / "config" / "devices.yaml"
        manager = get_device_manager(str(config_path))
        
        # 如果没有指定设备，使用第一个设备
        if device_id is None:
            devices = manager.get_all_devices()
            if not devices:
                logger.error("没有可用的设备")
                return False
            
            device_id = devices[0].device_id
            logger.info(f"使用设备: {devices[0].display_name}")
        
        # 设置保存路径
        if save_path is None:
            timestamp = Path(__file__).stem
            save_path = f"logs/screenshots/screenshot_{timestamp}.png"
        
        logger.info(f"正在截取设备 {device_id} 的屏幕...")
        screenshot_data = manager.capture_screen(device_id, save_path)
        
        if screenshot_data:
            logger.info(f"截图成功，保存到: {save_path}")
            print(f"截图已保存: {save_path}")
            print(f"文件大小: {len(screenshot_data)} bytes")
            return True
        else:
            logger.error("截图失败")
            return False
            
    except Exception as e:
        logger.error(f"截图失败: {e}")
        return False


def test_input(device_id=None):
    """测试输入功能"""
    logger = logging.getLogger(__name__)
    
    try:
        config_path = project_root / "config" / "devices.yaml"
        manager = get_device_manager(str(config_path))
        
        # 如果没有指定设备，使用第一个设备
        if device_id is None:
            devices = manager.get_all_devices()
            if not devices:
                logger.error("没有可用的设备")
                return False
            
            device_id = devices[0].device_id
            device_name = devices[0].display_name
        
        logger.info(f"在设备 {device_name} 上测试输入功能...")
        
        # 测试点击
        print("测试点击屏幕中心...")
        success = manager.input_tap(device_id, 500, 500)
        print(f"点击测试: {'成功' if success else '失败'}")
        
        # 测试文本输入
        print("测试文本输入...")
        success = manager.input_text(device_id, "Hello Mobile Auto!")
        print(f"文本输入测试: {'成功' if success else '失败'}")
        
        # 测试返回键
        print("测试返回键...")
        success = manager.input_keyevent(device_id, 4)  # KEYCODE_BACK
        print(f"返回键测试: {'成功' if success else '失败'}")
        
        return True
        
    except Exception as e:
        logger.error(f"输入测试失败: {e}")
        return False


def get_device_info(device_id):
    """获取设备详细信息"""
    logger = logging.getLogger(__name__)
    
    try:
        config_path = project_root / "config" / "devices.yaml"
        manager = get_device_manager(str(config_path))
        
        device_info = manager.get_device_info(device_id)
        
        if not device_info:
            logger.error(f"设备不存在: {device_id}")
            return False
        
        print(f"\n设备详细信息:")
        print("=" * 80)
        print(f"设备ID: {device_info.device_id}")
        print(f"显示名称: {device_info.display_name}")
        print(f"平台: {device_info.platform}")
        print(f"制造商: {device_info.manufacturer}")
        print(f"型号: {device_info.model}")
        print(f"Android版本: {device_info.android_version}")
        print(f"状态: {device_info.status.value}")
        
        if device_info.resolution:
            print(f"分辨率: {device_info.resolution.get('width')}x{device_info.resolution.get('height')}")
        
        print(f"DPI: {device_info.dpi}")
        print(f"最后检测时间: {device_info.last_seen}")
        
        # 获取当前Activity
        activity = manager.get_current_activity(device_id)
        if activity:
            print(f"当前Activity: {activity}")
        
        return True
        
    except Exception as e:
        logger.error(f"获取设备信息失败: {e}")
        return False


def serve_api(host: str = "0.0.0.0", port: int = 18080):
    """启动 FastAPI 服务器"""
    import uvicorn
    from src.host.api import app
    from src.host.database import init_db

    init_db()
    print(f"OpenClaw API 启动: http://{host}:{port}")
    print(f"文档: http://{host}:{port}/docs")
    uvicorn.run(app, host=host, port=port, log_level="info")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='OpenClaw 手机自动化工具')
    parser.add_argument('--verbose', '-v', action='store_true', help='详细输出')
    parser.add_argument('--config', '-c', default='config/devices.yaml', help='配置文件路径')

    subparsers = parser.add_subparsers(dest='command', help='子命令')

    subparsers.add_parser('list', help='列出所有设备')

    capture_p = subparsers.add_parser('capture', help='截取屏幕')
    capture_p.add_argument('--device', '-d', help='设备ID')
    capture_p.add_argument('--output', '-o', help='输出文件路径')

    test_p = subparsers.add_parser('test', help='测试输入功能')
    test_p.add_argument('--device', '-d', help='设备ID')

    info_p = subparsers.add_parser('info', help='获取设备信息')
    info_p.add_argument('device', help='设备ID')

    serve_p = subparsers.add_parser('serve', help='启动 API 服务器')
    serve_p.add_argument('--host', default='0.0.0.0', help='监听地址')
    serve_p.add_argument('--port', type=int, default=18080, help='端口')

    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    setup_logging(log_level)
    Path("logs/screenshots").mkdir(parents=True, exist_ok=True)

    if args.command == 'serve':
        serve_api(args.host, args.port)
    elif args.command == 'list':
        success = list_devices()
        sys.exit(0 if success else 1)
    elif args.command == 'capture':
        success = capture_screen(args.device, args.output)
        sys.exit(0 if success else 1)
    elif args.command == 'test':
        success = test_input(args.device)
        sys.exit(0 if success else 1)
    elif args.command == 'info':
        success = get_device_info(args.device)
        sys.exit(0 if success else 1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()