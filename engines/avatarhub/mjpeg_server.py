# -*- coding: utf-8 -*-
"""
把 OBS Virtual Camera 的换脸画面通过 MJPEG HTTP 推出去
手机浏览器打开 http://[电脑IP]:8080 即可看到换脸视频
同时输出 RTSP 给虚拟摄像头 App（如 USB Camera, RTSP Camera等）
"""
import sys, io, cv2, threading, time, socket
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8080
_latest_jpeg = None
_lock = threading.Lock()

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()

def capture_loop():
    global _latest_jpeg
    cap = cv2.VideoCapture(1, cv2.CAP_DSHOW)  # OBS Virtual Camera
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 480)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 854)
    print(f"[Capture] OBS Virtual Camera opened={cap.isOpened()}", flush=True)
    while True:
        ret, frame = cap.read()
        if not ret or frame is None:
            time.sleep(0.05)
            continue
        # 旋转为竖屏（如果是横屏）
        h, w = frame.shape[:2]
        if w > h:
            frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
        _, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        with _lock:
            _latest_jpeg = jpeg.tobytes()
        time.sleep(1/15)

class MJPEGHandler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass  # 静默日志

    def do_GET(self):
        if self.path == '/':
            html = f'''<!DOCTYPE html><html><head><title>FaceSwap Live</title>
            <style>body{{margin:0;background:#000;display:flex;justify-content:center;align-items:center;height:100vh}}
            img{{max-height:100vh;max-width:100vw}}</style></head>
            <body><img src="/stream" /></body></html>'''
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(html.encode())
        elif self.path == '/stream':
            self.send_response(200)
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
            self.end_headers()
            try:
                while True:
                    with _lock:
                        jpeg = _latest_jpeg
                    if jpeg:
                        self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\n\r\n')
                        self.wfile.write(jpeg)
                        self.wfile.write(b'\r\n')
                    time.sleep(1/15)
            except Exception:
                pass
        else:
            self.send_response(404); self.end_headers()

if __name__ == "__main__":
    t = threading.Thread(target=capture_loop, daemon=True)
    t.start()
    time.sleep(2)  # 等待第一帧

    ip = get_local_ip()
    print(f"\n{'='*50}", flush=True)
    print(f"  MJPEG 直播服务已启动", flush=True)
    print(f"  手机浏览器打开：http://{ip}:{PORT}", flush=True)
    print(f"  或扫码：http://{ip}:{PORT}/stream (MJPEG流)", flush=True)
    print(f"{'='*50}\n", flush=True)

    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(f"http://{ip}:{PORT}")
        qr.make(fit=True)
        qr.print_ascii()
    except ImportError:
        pass

    server = HTTPServer(('0.0.0.0', PORT), MJPEGHandler)
    print(f"[Server] Listening on 0.0.0.0:{PORT}  Ctrl+C to stop", flush=True)
    server.serve_forever()
