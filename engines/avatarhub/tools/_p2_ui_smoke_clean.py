# -*- coding: utf-8 -*-
"""UI 冒烟清场：删除测试写入的幽灵偏好，恢复无偏好初始态。"""
import os
p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "audio_prefs.json")
try:
    os.remove(p)
    print("removed", p)
except FileNotFoundError:
    print("already clean")
