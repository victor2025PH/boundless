"""
手机摄像头 MJPEG HTTP 服务器
通过 ADB 持续截帧，对外提供 http://127.0.0.1:8080/cam.mjpg
OBS 用"媒体源"订阅此 URL 即可获得实时画面
"""
import ctypes
import ctypes.wintypes
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from io import BytesIO
from PIL import ImageGrab, Image

DEVICE_ID = "JZBIGUKZS4NBAYDI"   # USB ADB 设备ID
WINDOW_TITLE = "PhoneCam"   # scrcpy 窗口标题
PORT = 8080
FPS = 15
DELAY = 1.0 / FPS

_lock = threading.Lock()
_frame = b""
_clients = 0
_user32 = ctypes.windll.user32


def get_window_rect(title):
    hwnd = _user32.FindWindowW(None, title)
    if not hwnd:
        return None, None
    r = ctypes.wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(r))
    if r.left < -10000:  # 最小化状态
        _user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.5)
        _user32.GetWindowRect(hwnd, ctypes.byref(r))
    return hwnd, (r.left, r.top, r.right, r.bottom)


def capture_loop():
    global _frame
    print(f"[cam] 开始捕获窗口 '{WINDOW_TITLE}' ({FPS} FPS)...")
    fail_count = 0
    while True:
        try:
            hwnd, rect = get_window_rect(WINDOW_TITLE)
            if not hwnd:
                if fail_count % 30 == 0:
                    print(f"[cam] 未找到窗口 '{WINDOW_TITLE}'，等待 scrcpy 启动...")
                fail_count += 1
                time.sleep(1)
                continue

            w = rect[2] - rect[0]
            h = rect[3] - rect[1]
            if w < 10 or h < 10:
                time.sleep(0.5)
                continue

            # 裁掉标题栏（约30px）
            crop = (rect[0], rect[1] + 30, rect[2], rect[3])
            img = ImageGrab.grab(crop)
            # 缩放到 640x480
            img = img.resize((640, 480), Image.LANCZOS)
            buf = BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=80)
            with _lock:
                _frame = buf.getvalue()
            fail_count = 0
        except Exception as e:
            fail_count += 1
            if fail_count % 10 == 0:
                print(f"[cam] 捕获异常: {e}")
        time.sleep(DELAY)


class MJPEGHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        global _clients
        if self.path == "/cam.mjpg":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            _clients += 1
            print(f"[cam] 客户端连接，当前: {_clients}")
            try:
                while True:
                    with _lock:
                        data = _frame
                    if data:
                        header = (
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n" +
                            f"Content-Length: {len(data)}\r\n\r\n".encode()
                        )
                        self.wfile.write(header)
                        self.wfile.write(data)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                    time.sleep(DELAY)
            except Exception:
                pass
            finally:
                _clients -= 1
                print(f"[cam] 客户端断开，当前: {_clients}")
        elif self.path == "/snapshot.jpg":
            with _lock:
                data = _frame
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/health":
            self.send_response(200)
            self.end_headers()
            with _lock:
                sz = len(_frame)
            self.wfile.write(f"OK frame={sz}bytes clients={_clients}".encode())
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    t = threading.Thread(target=capture_loop, daemon=True)
    t.start()

    print(f"[cam] 等待首帧...")
    for _ in range(20):
        with _lock:
            if _frame:
                break
        time.sleep(0.5)

    server = ThreadingHTTPServer(("0.0.0.0", PORT), MJPEGHandler)
    print(f"[cam] MJPEG 服务已启动:")
    print(f"      实时流: http://127.0.0.1:{PORT}/cam.mjpg")
    print(f"      单帧:   http://127.0.0.1:{PORT}/snapshot.jpg")
    print(f"      健康:   http://127.0.0.1:{PORT}/health")
    print(f"[cam] OBS 媒体源填入: http://127.0.0.1:{PORT}/cam.mjpg")
    server.serve_forever()
