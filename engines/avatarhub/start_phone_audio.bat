@echo off
chcp 65001 >nul
title 手机音频输入桥接 (DroidCam + RVC)

echo ============================================
echo   手机音频输入桥接 - 单手机完成音视频输入
echo ============================================
echo.
echo [功能说明]
echo   - 视频输入: scrcpy 或 DroidCam 视频
echo   - 音频输入: DroidCam 音频 (手机麦克风)
echo   - 音频输出: RVC 变声后 -^> VB-Cable -^> 直播软件
echo.
echo [前置要求]
echo   1. 手机安装 DroidCam App
echo   2. PC安装 DroidCam Client
echo   3. 手机和PC在同一WiFi网络
echo   4. 安装 VB-Audio Virtual Cable (虚拟音频输出)
echo.
echo [使用步骤]
echo   1. 手机打开DroidCam App，记录IP地址
echo   2. PC端DroidCam Client连接手机IP
echo   3. 确认连接成功后按任意键继续...
echo.
pause
echo.
echo [检测音频设备]...
python phone_control.py audio
echo.
echo [配置RVC]...
echo 请在RVC实时变声界面中设置:
echo   输入设备: DroidCam Audio (MME)
echo   输出设备: CABLE Input (VB-Audio Virtual Cable) (MME)
echo   采样率:   44100Hz
echo   F0方法:   pm
echo.
echo [测试手机音频输入]...
python phone_control.py audio_setup
echo.
echo ============================================
echo 配置完成！现在可以:
echo   1. 启动RVC实时变声
echo   2. 手机说话，PC端能听到变声后音频
echo   3. 配合scrcpy实现单手机音视频输入
echo ============================================
pause
