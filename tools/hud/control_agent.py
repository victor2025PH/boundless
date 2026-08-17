# -*- coding: utf-8 -*-
"""control_agent —— 隔空受控·每机输入注入器（P2 · 2026-08-10）。

出处《隔空拉屏与远端手势操控_五视角方案_20260809.md》§六 P1/P2、§九决策点（2026-08-10
主人拍板：P2 精确点按 + 走 ops_gateway 全护栏 + 刷脸 + 仅脸备/口型/声备三台开发机）。

定位与安全边界（务必看懂再动）：
  · 这是**全项目唯一**会往本机注入鼠标/键盘的代码。与只读的 desktop_agent 分家正是纪律：
    只读机只装 desktop_agent（无注入代码=文件级铁证）；**本文件的存在=该机被主人显式允许受控**。
    生产直播链（中枢/韵声/听写）**永不部署本文件**。
  · **零主动权**：本 agent 自己不决定动什么。它只做一件事——带本机密钥轮询中枢
    GET /api/ctrl/pull；只有当中枢**当前授予**本机控制会话（刷脸武装通过 + 未推流 +
    总闸未关 + 未熔断，判据全在服务端 ops_gateway）时，pull 才回 granted:true + 待办动作，
    本 agent 才注入。任何异常/取不到/未授予 → 一个字节都不注入（fail-closed）。
  · **密钥闸**：部署时写本机专属密钥（secrets/ctrl_agents.json 存同一份）。pull 带
    X-Ctrl-Secret；密钥不符 → 服务端不返任何动作。防「谁都能 POST 让某机乱点」。
  · 坐标绝对化：动作带归一化坐标 (nx,ny)∈[0,1]，映射到本机**主显示器**绝对像素
    （与 desktop_agent 抓 monitors[1] 主屏同源），故中枢镜像上指哪、这里点哪。
  · 自卫限速：本 agent 侧再夹一层 ≤MAX_EPS 事件/秒（防上游风暴/误识别雪崩，纵深防御）。

依赖：仅标准库 + ctypes（Windows user32.SendInput）——**零第三方依赖**，任何 Python 都能跑，
  跨异构集群 venv 零摩擦（比 desktop_agent 还轻，连 mss/Pillow 都不需要）。

用法：
  python control_agent.py [--machine <id>] [--hub http://192.168.0.176:7913]
  python control_agent.py --selftest      # 无害自测：只移动光标读回验证绝对映射，不点不打字
  哨兵/计划任务须跑在**交互会话**（schtasks /sc onlogon /it）——SendInput 要有真桌面。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import sys
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

HOST2ID = {
    "DESKTOP-SH6IM7V": "zhongshu", "DESKTOP-07NVNHG": "yunsheng",
    "MS-DEFYSOMWQYBZ": "shengbei", "XTZJ-2026WERVDN": "lianbei",
    "XTZJ-2026YXDYXU": "tingxie", "XTW-20250807FDX": "kouxing",
}
POLL_S = 0.12               # 轮询周期（未授予时同样节奏，纯问一句，零注入）
MAX_EPS = 40                # 自卫限速：每秒最多注入事件数（纵深防御）
TYPE_MAX = 240              # 单次 type 文本上限（防注入超长串）
SECRET_FILE = Path(__file__).with_name("control_agent.secret")

# ---------------- Windows SendInput（ctypes；零第三方依赖） ----------------
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE = 0x0001, 0x8000
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_WHEEL = 0x0800
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004
VK = {"enter": 0x0D, "esc": 0x1B, "escape": 0x1B, "tab": 0x09, "backspace": 0x08,
      "delete": 0x2E, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
      "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "space": 0x20}


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send(inp: _INPUT) -> None:
    """唯一的系统注入出口——单测把它 monkeypatch 掉即可零副作用验证派发逻辑。"""
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> None:
    mi = _MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, None)
    _send(_INPUT(INPUT_MOUSE, _INPUTUNION(mi=mi)))


def _key_vk(vk: int, up: bool = False) -> None:
    ki = _KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, None)
    _send(_INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=ki)))


def _key_unicode(ch: str, up: bool = False) -> None:
    ki = _KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0), 0, None)
    _send(_INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=ki)))


def _clamp01(v) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


def move_abs(nx: float, ny: float) -> None:
    x = round(_clamp01(nx) * 65535)
    y = round(_clamp01(ny) * 65535)
    _mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, x, y)


def dispatch(ev: dict) -> bool:
    """把一条服务端下发的动作 → 具体注入。返回是否识别执行。
    这是「事件字典 → 注入」的唯一接缝：单测喂字典 + monkeypatch _send 即可断言映射正确。"""
    kind = str(ev.get("kind") or "")
    nx, ny = ev.get("nx"), ev.get("ny")
    if kind in ("move", "click", "dblclick", "rclick", "drag", "scroll") and nx is not None:
        move_abs(nx, ny)
    if kind == "move":
        return True
    if kind == "click":
        _mouse(MOUSEEVENTF_LEFTDOWN); _mouse(MOUSEEVENTF_LEFTUP); return True
    if kind == "dblclick":
        for _ in range(2):
            _mouse(MOUSEEVENTF_LEFTDOWN); _mouse(MOUSEEVENTF_LEFTUP)
        return True
    if kind == "rclick":
        _mouse(MOUSEEVENTF_RIGHTDOWN); _mouse(MOUSEEVENTF_RIGHTUP); return True
    if kind == "scroll":
        _mouse(MOUSEEVENTF_WHEEL, data=int(ev.get("dy") or 0) * 120)
        return True
    if kind == "drag":
        _mouse(MOUSEEVENTF_LEFTDOWN)
        move_abs(ev.get("nx2", nx), ev.get("ny2", ny))
        _mouse(MOUSEEVENTF_LEFTUP)
        return True
    if kind == "type":
        for ch in str(ev.get("text") or "")[:TYPE_MAX]:
            _key_unicode(ch); _key_unicode(ch, up=True)
        return True
    if kind == "key":
        vk = VK.get(str(ev.get("key") or "").lower())
        if vk is None:
            return False
        _key_vk(vk); _key_vk(vk, up=True)
        return True
    return False


# ---------------- 轮询主循环（fail-closed） ----------------

def _load_secret() -> str:
    return (os.environ.get("HUD_CTRL_SECRET") or "").strip() or (
        SECRET_FILE.read_text(encoding="utf-8").strip() if SECRET_FILE.is_file() else "")


def guess_machine() -> str:
    return HOST2ID.get(socket.gethostname().upper(), socket.gethostname().lower()[:24])


def pull(hub: str, mid: str, secret: str) -> dict:
    """带密钥问中枢：现在授权本机吗？有什么动作？取不到=空（fail-closed）。"""
    try:
        req = urllib.request.Request(f"{hub}/api/ctrl/pull?machine={mid}",
                                     headers={"X-Ctrl-Secret": secret})
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read() or b"{}")
    except Exception:
        return {}


def run(hub: str, mid: str, secret: str) -> int:
    print(f"control_agent machine={mid} hub={hub} (INPUT INJECTION; server-gated, fail-closed)")
    if not secret:
        print("no secret (HUD_CTRL_SECRET / control_agent.secret) -> refuse to run")
        return 2
    granted = False
    win_start, win_count = time.time(), 0
    while True:
        d = pull(hub, mid, secret)
        g = bool(d.get("granted"))
        if g != granted:
            print("granted -> control session live" if g else "control session ended -> idle")
            granted = g
        evs = d.get("events") if g else None
        if isinstance(evs, list):
            for ev in evs:
                now = time.time()
                if now - win_start >= 1.0:
                    win_start, win_count = now, 0
                if win_count >= MAX_EPS:            # 自卫限速：本秒已满，丢弃余下
                    break
                win_count += 1
                try:
                    dispatch(ev)
                except Exception as e:              # noqa: BLE001  单条动作异常不拖垮 agent
                    print("dispatch error:", type(e).__name__, str(e)[:80])
        time.sleep(POLL_S)


def selftest() -> int:
    """无害自测：只移动光标到两个绝对点并读回，验证 nx,ny→主屏像素映射。不点不打字。"""
    u = ctypes.windll.user32
    W, H = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    pt = wintypes.POINT()
    ok = True
    for nx, ny in ((0.5, 0.5), (0.3, 0.7)):
        move_abs(nx, ny)
        time.sleep(0.05)
        u.GetCursorPos(ctypes.byref(pt))
        ex, ey = round(nx * (W - 1)), round(ny * (H - 1))
        dx, dy = abs(pt.x - ex), abs(pt.y - ey)
        good = dx <= max(3, W // 300) and dy <= max(3, H // 300)
        ok = ok and good
        print(f"  move({nx},{ny}) -> ({pt.x},{pt.y}) expect~({ex},{ey}) d=({dx},{dy}) {'OK' if good else 'FAIL'}")
    print(f"selftest screen={W}x{H} : {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="control_agent 受控输入注入器（服务端判决/fail-closed）")
    ap.add_argument("--machine", default="")
    ap.add_argument("--hub", default="http://127.0.0.1:7913")
    ap.add_argument("--selftest", action="store_true", help="无害自测：只移动光标读回")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    return run(a.hub.rstrip("/"), (a.machine or guess_machine()).strip().lower(), _load_secret())


if __name__ == "__main__":
    sys.exit(main())
