#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨机显存互斥 · B 端包装器（零三方依赖，仅标准库）。

在 B 机用本脚本「包裹」你的 GPU 程序：运行期间独占 A 机(avatar_hub)的整张显卡——
启动前通知 A 清空显存(停服务 + 卸本地 LLM)并等到真的腾空，B 程序退出后通知 A 恢复服务。
B 崩溃/断电导致没正常归还也无妨：A 端心跳租约超时会自动回收并恢复服务（绝不让 A 永久躺平）。

用法:
  python gpu_peer_lease.py --hub http://A主机:9000 --token <AVATARHUB_API_TOKEN> \\
      --need-mb 28000 --ttl 30 --wait 1800 -- <你的GPU程序> [参数...]

  # 不带命令(仅占卡, Ctrl-C 归还)——适合手动包裹一个单独启动的 GUI:
  python gpu_peer_lease.py --hub http://A主机:9000 --token XXX --hold

参数:
  --hub      A 机 Hub 基址(http://ip:9000)              [必填]
  --token    A 机 AVATARHUB_API_TOKEN(跨机写操作鉴权)   [A 设了 token 则必填]
  --need-mb  需要的空闲显存(MB); 缺省=A 总显存 ~90%(确认确已腾空)
  --ttl      租约时长(秒, 默认 30); 每 ttl/3 秒自动续约
  --wait     A 忙(直播/被他人占)时最长重试等待(秒, 默认 1800); 0=不等待直接失败
  --force    A 直播中也强制抢占(需 A 端 AVATARHUB_PEER_ALLOW_FORCE=1; 会打断直播)
  --holder   本机标识(默认本机名), 显示在 A 的 /ops 看板上
  --hold     不运行命令, 仅占卡直到 Ctrl-C

退出码 = 被包裹程序的退出码（--hold 模式恒 0）。
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

RETRY_SEC = 5.0   # A 忙时重试间隔


def _req(method, url, token, data=None, timeout=30):
    """发一个请求，返回 (http_status, json_dict)。网络异常 status=0。"""
    body = json.dumps(data).encode("utf-8") if data is not None else None
    headers = {"X-AH-Token": token or ""}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            return e.code, {"detail": str(e)}
    except Exception as e:
        return 0, {"detail": str(e)}


class Lease:
    def __init__(self, hub, token):
        self.hub = hub.rstrip("/")
        self.token = token
        self.id = None
        self.ttl = 30
        self._stop = threading.Event()
        self._hb = None

    def acquire(self, need_mb, ttl, holder, force, wait):
        qs = ("?need_mb=%d&ttl=%d&holder=%s&force=%s"
              % (need_mb, ttl, urllib.parse.quote(holder), "true" if force else "false"))
        url = self.hub + "/api/gpu/lease/acquire" + qs
        deadline = time.time() + max(0, wait)
        while True:
            code, j = _req("POST", url, self.token, data={}, timeout=ttl + 40)
            if code == 200 and j.get("ok"):
                self.id = j.get("lease_id")
                self.ttl = j.get("ttl", ttl)
                return j
            if code == 401:
                raise RuntimeError("未授权(HTTP 401)：A 设置了 AVATARHUB_API_TOKEN，请用 --token 传同一个令牌")
            if code == 409 and time.time() < deadline:
                print("[lease] A 暂不可用(%s)，%gs 后重试…" % (j.get("detail", "busy"), RETRY_SEC), flush=True)
                time.sleep(RETRY_SEC)
                continue
            raise RuntimeError("acquire 失败 HTTP %s: %s" % (code, j.get("detail")))

    def _heartbeat_loop(self, need_mb, holder, force):
        interval = max(2.0, self.ttl / 3.0)
        while not self._stop.wait(interval):
            code, j = _req("POST", self.hub + "/api/gpu/lease/heartbeat",
                           self.token, data={"lease_id": self.id}, timeout=15)
            if code == 200:
                continue
            # 租约没了(A 重启/被回收/被运维强制收回) → 立即重新申请，恢复互斥独占
            print("[lease] 心跳失败 HTTP %s: %s → 尝试重新申请" % (code, j.get("detail")), flush=True)
            try:
                self.acquire(need_mb, self.ttl, holder, force, wait=60)
                print("[lease] 重新申请成功 lease=%s" % self.id, flush=True)
            except Exception as e:
                print("[lease] 重新申请失败: %s" % e, flush=True)

    def start_heartbeat(self, need_mb, holder, force):
        self._hb = threading.Thread(target=self._heartbeat_loop,
                                    args=(need_mb, holder, force), daemon=True)
        self._hb.start()

    def release(self):
        self._stop.set()
        if not self.id:
            return
        code, j = _req("POST", self.hub + "/api/gpu/lease/release",
                       self.token, data={"lease_id": self.id}, timeout=60)
        print("[lease] 归还显卡 HTTP %s (%s) → A 正在恢复服务" %
              (code, j.get("detail") or j.get("was_holder") or "ok"), flush=True)
        self.id = None


def main():
    # 手动按首个 "--" 切分：之前是本脚本选项，之后是被包裹的命令
    if "--" in sys.argv:
        i = sys.argv.index("--")
        opt, cmd = sys.argv[1:i], sys.argv[i + 1:]
    else:
        opt, cmd = sys.argv[1:], []
    ap = argparse.ArgumentParser(description="跨机显存互斥 · B 端包装器", add_help=True)
    ap.add_argument("--hub", required=True, help="A 机 Hub 基址，如 http://192.168.1.10:9000")
    ap.add_argument("--token", default=os.environ.get("AVATARHUB_API_TOKEN", ""), help="A 机 API 令牌")
    ap.add_argument("--need-mb", type=int, default=0, dest="need_mb", help="需要的空闲显存MB(缺省 A 总显存~90%%)")
    ap.add_argument("--ttl", type=int, default=30, help="租约秒数(默认30)，每 ttl/3 秒续约")
    ap.add_argument("--wait", type=int, default=1800, help="A 忙时最长重试等待秒(默认1800)")
    ap.add_argument("--force", action="store_true", help="A 直播中也强制抢占(需 A 端开 ALLOW_FORCE)")
    ap.add_argument("--holder", default=socket.gethostname(), help="本机标识(默认本机名)")
    ap.add_argument("--hold", action="store_true", help="不运行命令，仅占卡直到 Ctrl-C")
    a = ap.parse_args(opt)
    if not cmd and not a.hold:
        ap.error("请在 -- 后给出要运行的 GPU 程序，或用 --hold 仅占卡")

    lease = Lease(a.hub, a.token)
    print("[lease] 申请 A(%s) 显卡: need_mb=%s ttl=%ds holder=%s"
          % (a.hub, a.need_mb or "auto~90%", a.ttl, a.holder), flush=True)
    j = lease.acquire(a.need_mb, a.ttl, a.holder, a.force, a.wait)
    print("[lease] 已获显卡 lease=%s 空闲=%sMB 等待=%sms enough=%s"
          % (lease.id, j.get("free_mb"), j.get("waited_ms"), j.get("freed_enough")), flush=True)
    if not j.get("freed_enough"):
        print("[lease] ⚠ 警告: 空闲显存可能未达 need_mb(A 释放慢或仍被占)，继续运行但请留意 OOM", flush=True)
    lease.start_heartbeat(a.need_mb, a.holder, a.force)

    rc = 0
    try:
        if cmd:
            print("[lease] 运行被包裹程序: %s" % " ".join(cmd), flush=True)
            rc = subprocess.call(cmd)
        else:
            print("[lease] --hold: 占卡中，按 Ctrl-C 归还…", flush=True)
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        print("[lease] 收到中断，归还显卡…", flush=True)
    finally:
        try:
            lease.release()
        except Exception as e:
            print("[lease] 归还异常(A 端 TTL 会兜底回收): %s" % e, flush=True)
    sys.exit(rc)


if __name__ == "__main__":
    main()
