# -*- coding: utf-8 -*-
"""半死演练用的假池：/api/health 一直 200，但 allocate 挂死（永远不回）。

复刻 2026-07-14 EmotionTTS 那次事故的形状——端口活着、健康检查全绿、
业务口全部超时。用来验证看门狗的业务探针能不能抓到它。
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


class H(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/health"):
            self._json(200, {"status": "ok", "server": "credpool", "persist": True})
        else:
            self._json(404, {})

    def do_POST(self):  # noqa: N802
        # 业务口装死：不回、不断，就这么吊着（正是最难被 health 抓到的形态）
        time.sleep(120)
        self._json(500, {"success": False, "message": "hang"})

    def log_message(self, *a):  # 静音
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    print(f"假池（health 绿 / 业务装死）监听 {port}", flush=True)
    HTTPServer(("127.0.0.1", port), H).serve_forever()
