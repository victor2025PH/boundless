"""
HTTPS 反向代理 —— 让手机能在安全上下文(HTTPS)下访问数字人，从而开放麦克风/摄像头权限。
浏览器仅在 HTTPS 或 localhost 才允许 getUserMedia；手机经局域网 IP 走 HTTP 会被拦截。

监听: 0.0.0.0:9443 (HTTPS, 自签证书) → 转发到 http://127.0.0.1:9000 (Hub)
支持: 普通请求 + 流式响应(SSE，对话流必需) + 大 body 上传(音频)

手机访问: https://<本机IP>:9443/static/phone.html  （首次需点“仍要继续/接受风险”）
运行环境: facefusion (cryptography + starlette + httpx + uvicorn)
"""
import os, sys, socket, datetime, ipaddress, logging
import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import StreamingResponse, Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s [HTTPS-PROXY] %(message)s")
logger = logging.getLogger("proxy")

UPSTREAM = os.environ.get("HUB_UPSTREAM", "http://127.0.0.1:9000")
CERT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_proxy_cert.pem")
KEY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_proxy_key.pem")
PORT = int(os.environ.get("PROXY_PORT", "9443"))

# 逐跳头不应转发
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}


def _lan_ips():
    ips = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            if ":" not in ip:
                ips.add(ip)
    except Exception:
        pass
    return ips


def _ensure_cert():
    if os.path.exists(CERT) and os.path.exists(KEY):
        return
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    logger.info("生成自签证书…")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ips = _lan_ips()
    san = [x509.DNSName("localhost")]
    for ip in ips:
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except Exception:
            pass
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "avatarhub-local")])
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.utcnow() - datetime.timedelta(days=1))
            .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .sign(key, hashes.SHA256()))
    with open(KEY, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    with open(CERT, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    logger.info(f"证书已生成，覆盖 IP: {sorted(ips)}")


_client: httpx.AsyncClient = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        # 关闭响应超时(SSE 长连接)；连接超时保留
        _client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0))
    return _client


async def _proxy(request):
    _client = _get_client()
    url = UPSTREAM + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    headers = [(k, v) for k, v in request.headers.items() if k.lower() not in _HOP]
    body = await request.body()

    req = _client.build_request(request.method, url, headers=headers, content=body)
    resp = await _client.send(req, stream=True)
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP}

    async def _body():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(_body(), status_code=resp.status_code,
                             headers=out_headers,
                             media_type=resp.headers.get("content-type"))


app = Starlette(routes=[Route("/{path:path}", _proxy,
                              methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])])


if __name__ == "__main__":
    _ensure_cert()
    print(f"[HTTPS-PROXY] 手机访问: https://<本机IP>:{PORT}/static/phone.html  (LAN IP 见 ipconfig)")
    uvicorn.run(app, host="0.0.0.0", port=PORT, ssl_certfile=CERT, ssl_keyfile=KEY, log_level="warning")
