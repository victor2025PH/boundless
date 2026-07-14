# -*- coding: utf-8 -*-
"""UI 冒烟前置：写入一个『缺席』输入偏好，让开播页出现 P2-1 驻留警告条。"""
import requests
r = requests.post("http://127.0.0.1:9000/api/audio/prefs",
                  json={"input": "麦克风 (Ghost USB Device) (MME)"}, timeout=10).json()
print("prefs:", r)
