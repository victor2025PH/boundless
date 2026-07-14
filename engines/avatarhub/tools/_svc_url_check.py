# -*- coding: utf-8 -*-
"""裸终端(未 call env_config.bat)下 svc_url 应回退 bat 静态默认：打印关键服务解析结果。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app_config

for k in ("stt", "nemo_stt", "faceswap", "lipsync", "emotion_tts", "qwen3_tts", "fish_tts", "vcam"):
    print(f"{k:12s} -> {app_config.svc_url(k)}")
