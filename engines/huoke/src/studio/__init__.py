# -*- coding: utf-8 -*-
"""Content Studio — AI内容生成与自动发布系统。"""

from .studio_db import init_studio_db

# 模块加载时自动初始化数据库
try:
    init_studio_db()
except Exception:
    pass
