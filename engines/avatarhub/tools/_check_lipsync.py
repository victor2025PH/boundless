# -*- coding: utf-8 -*-
"""看 lipsync 池状态 + 本机 8090 是否在听。"""
import json
import socket
import urllib.request

try:
    d = json.loads(urllib.request.urlopen("http://127.0.0.1:9000/api/services", timeout=8).read().decode())
    svc = (d.get("services") or {})
    for k in ("lipsync",):
        print(k, "=>", json.dumps(svc.get(k), ensure_ascii=False))
except Exception as e:
    print("services err:", e)

for port in (8090,):
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", port))
        print(f"port {port}: LISTENING")
    except Exception as e:
        print(f"port {port}: closed ({e})")
    finally:
        s.close()

try:
    d = json.loads(urllib.request.urlopen("http://127.0.0.1:9000/api/pool/status", timeout=8).read().decode())
    print("pool:", json.dumps(d, ensure_ascii=False)[:600])
except Exception as e:
    print("pool err:", e)
