"""PyInstaller 入口：agent.py 用包内相对导入，不能直接当脚本打。"""

import sys

from src.fleet.agent import main

if __name__ == "__main__":
    sys.exit(main())
