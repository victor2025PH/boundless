# -*- coding: utf-8 -*-
"""
终端聊天界面 — 通过命令行与 OpenClaw 交互。

启动: python -m src.chat.terminal_chat
"""

from __future__ import annotations

import sys
import os

from src.host.device_registry import PROJECT_ROOT

sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("PYTHONUNBUFFERED", "1")

from src.chat.controller import ChatController


BANNER = r"""
  ╔══════════════════════════════════════════════╗
  ║     OpenClaw Chat Control                    ║
  ║     自然语言控制手机自动化                      ║
  ╠══════════════════════════════════════════════╣
  ║  输入中文指令控制手机，例如:                     ║
  ║    > 01号手机养号30分钟                        ║
  ║    > 所有手机开始养号                           ║
  ║    > 哪些手机在线                              ║
  ║    > VPN状态                                  ║
  ║    > 帮助                                     ║
  ║                                              ║
  ║  输入 exit/quit 退出                          ║
  ╚══════════════════════════════════════════════╝
"""


def _colored(text: str, color: str) -> str:
    colors = {
        "green": "\033[92m",
        "blue": "\033[94m",
        "yellow": "\033[93m",
        "red": "\033[91m",
        "cyan": "\033[96m",
        "bold": "\033[1m",
        "dim": "\033[2m",
        "reset": "\033[0m",
    }
    return f"{colors.get(color, '')}{text}{colors['reset']}"


def main():
    print(_colored(BANNER, "cyan"))

    ctrl = ChatController()

    while True:
        try:
            user_input = input(_colored("\n> ", "green")).strip()
        except (EOFError, KeyboardInterrupt):
            print(_colored("\n再见!", "yellow"))
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "q", "退出"):
            print(_colored("再见!", "yellow"))
            break
        if user_input.lower() in ("clear", "cls", "清屏"):
            os.system("cls" if os.name == "nt" else "clear")
            ctrl.clear()
            continue

        print(_colored("  处理中...", "dim"))

        result = ctrl.handle(user_input)

        print()
        intent_str = _colored(f"[{result['intent']}]", "blue")
        elapsed_str = _colored(f"({result['elapsed_ms']}ms)", "dim")
        print(f"  {intent_str} {elapsed_str}")

        if result.get("task_ids"):
            for tid in result["task_ids"]:
                print(_colored(f"  Task: {tid[:12]}...", "dim"))

        reply_lines = result["reply"].split("\n")
        for line in reply_lines:
            if line.startswith("[失败]") or "error" in line.lower():
                print(f"  {_colored(line, 'red')}")
            else:
                print(f"  {_colored(line, 'yellow')}")


if __name__ == "__main__":
    main()
