# -*- coding: utf-8 -*-
"""TIME_WAIT vs SO_EXCLUSIVEADDRUSE 实证：服务端主动关连接产生 TIME_WAIT 后，
新监听者能否立刻重绑同端口？决定 realtime fail-fast 与 port_guard 会不会误伤快速重启。"""
import socket
import subprocess
import sys
import time

PORT = 19923


def netstat_states(port):
    out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True).stdout
    states = {}
    for ln in out.splitlines():
        p = ln.split()
        if len(p) >= 4 and (p[1].endswith(f":{port}") or p[2].endswith(f":{port}")):
            states[p[3]] = states.get(p[3], 0) + 1
    return states


def bind_try(opts, label):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    for o in opts:
        s.setsockopt(socket.SOL_SOCKET, o, 1)
    try:
        s.bind(("0.0.0.0", PORT))
        s.listen(1)
        print(f"  {label}: BIND OK")
        return s
    except OSError as e:
        print(f"  {label}: BIND FAIL WinError {e.winerror}")
        s.close()
        return None


def main():
    # 1) 监听者 A（无任何选项,与 _ExclusiveHTTPServer 的 nt 行为一致——EXCLUSIVE 只是显式化）
    a = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    a.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    a.bind(("0.0.0.0", PORT))
    a.listen(5)

    # 2) 模拟 hub 轮询：客户端连上,服务端先关(FIN 先手)→ 服务端侧 TIME_WAIT
    for _ in range(6):
        c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        c.connect(("127.0.0.1", PORT))
        conn, _ = a.accept()
        conn.shutdown(socket.SHUT_RDWR)   # 服务端主动关闭 → 本端进 TIME_WAIT
        conn.close()
        c.close()
    time.sleep(0.5)
    print("closing listener A; states:", netstat_states(PORT))
    a.close()
    time.sleep(0.5)
    print("after close; states:", netstat_states(PORT))

    # 3) 立刻重绑测试
    b = bind_try([socket.SO_EXCLUSIVEADDRUSE], "EXCLUSIVEADDRUSE rebind (TIME_WAIT ghosts present)")
    if b:
        b.close()
    d = bind_try([], "default-option rebind")
    if d:
        d.close()
    print("final states:", netstat_states(PORT))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
