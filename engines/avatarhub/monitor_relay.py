# -*- coding: utf-8 -*-
"""
monitor_relay.py — 手机监听中继（阶段1）

目标：把 PC 上"对方声"（通话App 的扬声器输出）通过 WiFi 实时送到手机放音，
      并把通译 LingoX 的中英双列字幕一并镜像到手机页面。
      → 手机用 DroidCam 当摄像头+麦(手机→PC)，本中继负责 PC→手机 的音频+字幕，
        从而真正"摄像头+麦+放音+字幕 全在一台手机上"。

设计要点（与现有同传/换脸完全解耦，独立进程、独立端口，零回归风险）：
  - WASAPI 环回(soundcard) 抓某个输出设备的声音 → PCM16 单声道 → WebSocket 广播。
  - 浏览器放音不需要 HTTPS（只有"采集"摄像头/麦才需要），故手机用 http 即可。
  - 字幕：通译 /events 绑在 127.0.0.1 手机够不到，这里用服务端流式代理成同源 /subs。
  - 传输用裸 PCM16（LAN 带宽足够、最稳、无编解码依赖）；Opus/WebRTC 留作后续优化。
  - 阶段2-E：手机摄像头 WebRTC 上行 → /cam.mjpeg 供 realtime_stream 当换脸源。

端口：7878 http / 7879 https（facefusion 环境）
依赖：fastapi uvicorn soundcard numpy httpx websockets opencv-python aiortc（facefusion 已具备）
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import queue
import re
import socket
import subprocess
import threading
import time
import warnings
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent

# soundcard 在被抓输出空闲(无人放音)时会刷 "data discontinuity in recording"，无害但会撑爆日志 → 静音它。
warnings.filterwarnings("ignore", message="data discontinuity in recording")
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [MONITOR] %(message)s")
logger = logging.getLogger("monitor_relay")

PORT = int(os.environ.get("MONITOR_PORT", "7878"))
HTTPS_PORT = int(os.environ.get("MONITOR_HTTPS_PORT", "7879"))   # 对讲(手机麦)需安全上下文→https
INTERP_URL = os.environ.get("INTERP_URL", "http://127.0.0.1:7900")
SR = int(os.environ.get("MONITOR_SR", "48000"))     # 环回采样率(Realtek 板载 48k 最稳)
FRAME = int(os.environ.get("MONITOR_FRAME", str(int(SR * 0.02))))  # 20ms 一帧
QCAP = 25                                            # 每客户端队列上限(~0.5s)，满则丢旧保低延迟
CAM_FPS = int(os.environ.get("MONITOR_CAM_FPS", "15"))
CAM_JPEG_Q = int(os.environ.get("MONITOR_CAM_JQ", "80"))

app = FastAPI(title="MonitorRelay")
# 服务面加固：手机是 LAN 直连的合法客户端（浏览器无法携带服务器令牌），故本服务的鉴权
# **不随共享令牌自动开启**（否则手机会被挡），需显式 MONITOR_AUTH=1 才打开；开启后请把手机
# IP/网段写入 secrets\service_allow_ips.txt，或让手机页面在 WS 上带 ?svc=<令牌>。默认关=手机零影响。
_MONITOR_AUTH = os.environ.get("MONITOR_AUTH", "").strip().lower() in ("1", "true", "yes", "on")
try:
    import service_auth
    if _MONITOR_AUTH:
        service_auth.secure(app, name="monitor")         # HTTP 面；WS 面由各 handler 内 ws_authorized 兜（HTTP 中间件不覆盖 WS）
except Exception as _e:
    service_auth = None
    logger.warning(f"service_auth 未启用: {_e}")

# ── 广播总线（PC→手机：放音）─────────────────────────────────────────
# 每个订阅者存 (queue, 它所属的事件循环)，因为 http/https 是两个 uvicorn 各带一个 loop，
# 跨 loop 必须用各自 loop 的 call_soon_threadsafe，否则投递无效/报错。
_subs: set[tuple[asyncio.Queue, asyncio.AbstractEventLoop]] = set()
_subs_lock = threading.Lock()

# ── 对讲总线（手机→PC：注入到虚拟声卡，供解释器当"本人麦"）──────────────
_mic_q: queue.Queue = queue.Queue(maxsize=50)        # 线程队列，跨 loop 安全
_mic_target: str | None = os.environ.get("MONITOR_MIC_OUT", "") or None
_mic_gen = 0
_mic_status = {"ok": False, "dev": "", "err": "", "level": 0.0, "frames": 0}
# 麦克风出口(tap)：解释器经 GET /mic/pcm 直连拉手机麦,免 VB-Cable 中转。与播放器各自独立队列(扇出)。
_mic_taps: set[queue.Queue] = set()
_mic_taps_lock = threading.Lock()

# 后台线程只起一次（http/https 两个 server 都会触发 startup 事件）
_bg_lock = threading.Lock()
_bg_started = False

# ── 摄像头总线（手机→PC：WebRTC/WS 上行 → MJPEG 给换脸）────────────────
_cam_frame: np.ndarray | None = None
_cam_lock = threading.Lock()
_cam_pcs: set = set()
_cam_status = {"connected": False, "frames": 0, "w": 0, "h": 0, "fps": 0.0,
               "source": "", "peers": 0, "err": ""}
_cam_fps_t0 = 0.0
_cam_fps_n = 0

# 采集控制：改设备时递增 _gen 让采集线程重开
_dev_name: str | None = None          # None=默认扬声器；否则按名字子串匹配
_gen = 0
_stop = threading.Event()
_cap_status = {"ok": False, "dev": "", "err": "", "frames": 0, "rms": 0.0}

# ── 网络环境态（换 WiFi/局域网→IP 变更时自动复检+刷新二维码）────────────────
# qr_gen：二维码/链接版本号，IP 每变一次 +1，页面据此自动换码并提示重扫。
_net_state = {"ip": "", "subnet": "", "qr_gen": 0, "checked_at": 0.0,
              "cert_ip_ok": None, "checks": {}}
_net_lock = threading.Lock()


def _put_drop(q: asyncio.Queue, data: bytes):
    """非阻塞入队；满则丢一个最旧帧再放(始终保最新，避免延迟堆积)。"""
    try:
        q.put_nowait(data)
    except asyncio.QueueFull:
        try:
            q.get_nowait()
            q.put_nowait(data)
        except Exception:
            pass


def _broadcast(data: bytes):
    with _subs_lock:
        targets = list(_subs)
    for q, loop in targets:
        try:
            loop.call_soon_threadsafe(_put_drop, q, data)
        except Exception:
            pass


def _resample_i16(i16: np.ndarray, src: int, dst: int) -> np.ndarray:
    """线性重采样 int16 单声道（手机麦常为 44.1k/16k，需对齐到环回 48k）。"""
    if src == dst or len(i16) == 0:
        return i16
    n = len(i16)
    m = max(1, int(round(n * dst / src)))
    xi = np.linspace(0, n - 1, m)
    out = np.interp(xi, np.arange(n), i16.astype(np.float32))
    return out.astype("<i2")


def _mic_ingest(raw: bytes, src_sr: int):
    """收到手机麦一帧 PCM16 → 重采样到 SR → 入注入队列(满则丢旧保低延迟)。"""
    i16 = np.frombuffer(raw, dtype="<i2")
    if not len(i16):
        return
    if src_sr != SR:
        i16 = _resample_i16(i16, src_sr, SR)
    _mic_status["level"] = float(np.abs(i16).max()) / 32768.0
    _mic_status["frames"] += 1
    payload = i16.tobytes()
    try:
        _mic_q.put_nowait(payload)
    except queue.Full:
        try:
            _mic_q.get_nowait()
            _mic_q.put_nowait(payload)
        except Exception:
            pass
    # 扇出到所有出口订阅者(解释器直连)。与播放器队列独立,互不抢帧;满则丢旧保低延迟。
    with _mic_taps_lock:
        taps = list(_mic_taps)
    for tq in taps:
        try:
            tq.put_nowait(payload)
        except queue.Full:
            try:
                tq.get_nowait()
                tq.put_nowait(payload)
            except Exception:
                pass


def _mic_player_supervisor():
    """常驻：把对讲队列里的 PCM 播放到选定的虚拟声卡(=解释器读它当本人麦)。
    未选目标设备时空转（零干预现有音频路由）。"""
    try:
        import soundcard as sc
    except Exception as e:
        _mic_status.update(ok=False, err=f"soundcard 不可用: {e}")
        return
    while not _stop.is_set():
        target = _mic_target
        if not target:
            _mic_status.update(ok=False, dev="")
            time.sleep(0.25)
            continue
        my_gen = _mic_gen
        try:
            spk = _pick_speaker(sc, target)
            _mic_status.update(ok=True, dev=spk.name, err="")
            logger.info(f"对讲注入启动 → '{spk.name}'(解释器把它选作本人麦)")
            with spk.player(samplerate=SR, channels=1, blocksize=FRAME) as pl:
                while not _stop.is_set() and my_gen == _mic_gen and _mic_target:
                    try:
                        chunk = _mic_q.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    f = np.frombuffer(chunk, dtype="<i2").astype(np.float32) / 32768.0
                    pl.play(f.reshape(-1, 1))
        except Exception as e:
            _mic_status.update(ok=False, err=str(e))
            logger.warning(f"对讲注入异常，1s 后重试: {e}")
            time.sleep(1.0)


# 本地根 CA(长期有效)：手机装一次并信任 → 对讲页(https)永久免证书告警；leaf 由它签发，换 WiFi 也不必重装。
CA_CERT = BASE / "_monitor_ca.pem"
CA_KEY = BASE / "_monitor_ca_key.pem"


def _ensure_cert():
    """确保存在覆盖当前 LAN IP 的 TLS leaf 证书(由本地 CA 签发；手机浏览器采麦需 https)。返回(cert,key)或 None。"""
    cf = BASE / "_monitor_cert.pem"
    kf = BASE / "_monitor_key.pem"
    ip = _lan_ip()
    try:
        from cryptography import x509
        if cf.exists() and kf.exists():       # 复用：仅当已覆盖当前 IP、未过期、且由现有本地 CA 签发
            try:
                c = x509.load_pem_x509_certificate(cf.read_bytes())
                san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
                ips = [str(a) for a in san.get_values_for_type(x509.IPAddress)]
                naf = getattr(c, "not_valid_after_utc", None) or c.not_valid_after
                import datetime as _dt
                now = _dt.datetime.now(_dt.timezone.utc) if naf.tzinfo else _dt.datetime.utcnow()
                # 旧版自签 leaf(无 CA 文件或非本 CA 签发)→ 视为需升级，触发一次重签换成 CA 背书。
                # 用验签(而非 issuer 名字)判断：CA 同名重建后密钥不同，名字相等但签不上→仍需重签。
                ca_ok = CA_CERT.exists() and _leaf_signed_by(
                    c, x509.load_pem_x509_certificate(CA_CERT.read_bytes()))
                if ip in ips and naf > now and ca_ok:
                    return str(cf), str(kf)
            except Exception:
                pass
        _gen_cert(cf, kf, ip)
        return str(cf), str(kf)
    except Exception as e:
        logger.warning(f"无法生成 TLS 证书({e})，对讲(https)不可用，仅 http 监听可用")
        return None


def _ensure_ca():
    """确保本地根 CA 存在(缺失即创建，长期有效)。返回 (ca_cert对象, ca_key对象)。
    CA 只用于签发 leaf + 供手机安装信任；私钥留在本机，绝不外发。"""
    import datetime as _dt
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    if CA_CERT.exists() and CA_KEY.exists():
        try:
            ca = x509.load_pem_x509_certificate(CA_CERT.read_bytes())
            ck = serialization.load_pem_private_key(CA_KEY.read_bytes(), password=None)
            return ca, ck
        except Exception:
            pass
    ck = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    host = socket.gethostname() or "PC"
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"AvatarHub Local CA ({host})"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "AvatarHub")])
    now = _dt.datetime.now(_dt.timezone.utc)
    ski = x509.SubjectKeyIdentifier.from_public_key(ck.public_key())
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
          .public_key(ck.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - _dt.timedelta(days=1))
          .not_valid_after(now + _dt.timedelta(days=3650))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                       content_commitment=False, key_encipherment=False,
                                       data_encipherment=False, key_agreement=False,
                                       encipher_only=False, decipher_only=False), critical=True)
          .add_extension(ski, critical=False)
          .sign(ck, hashes.SHA256()))
    CA_KEY.write_bytes(ck.private_bytes(serialization.Encoding.PEM,
                       serialization.PrivateFormat.TraditionalOpenSSL,
                       serialization.NoEncryption()))
    CA_CERT.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    logger.info(f"已生成本地根 CA → {CA_CERT.name}（手机装一次即免证书告警）")
    return ca, ck


def _gen_cert(cf: Path, kf: Path, ip: str):
    """生成由本地 CA 签发的 TLS leaf(SAN 含当前 IP)。服务端仍只出示此 leaf，握手行为与旧版一致；
    区别仅是签发者从'自签'变为'本地 CA'，从而手机装一次 CA 即可对 leaf 免告警。"""
    import datetime as _dt
    from cryptography import x509
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import ipaddress
    ca_cert, ca_key = _ensure_ca()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"monitor-relay {ip}")])
    san = [x509.DNSName("localhost")]
    for a in {"127.0.0.1", ip}:
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(a)))
        except Exception:
            pass
    now = _dt.datetime.now(_dt.timezone.utc)
    aki = x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key())
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(ca_cert.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(days=1))
            .not_valid_after(now + _dt.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(aki, critical=False)
            .sign(ca_key, hashes.SHA256()))
    kf.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                   serialization.PrivateFormat.TraditionalOpenSSL,
                   serialization.NoEncryption()))
    cf.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    logger.info(f"已签发 TLS leaf(CA 背书) SAN=[127.0.0.1,{ip},localhost] → {cf.name}")


def _lan_ip() -> str:
    """取本机在 LAN 上的真实 IPv4（UDP 连一下不实际发包，比 gethostbyname 可靠）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "127.0.0.1"


def _subnet_of(ip: str) -> str:
    p = (ip or "").split(".")
    return ".".join(p[:3]) + ".x" if len(p) == 4 else ""


def _leaf_signed_by(leaf, ca_cert) -> bool:
    """leaf 是否**确实由** ca_cert 的私钥签发（验签，而非仅比对 issuer 名字）。
    CA 主题名按主机名派生=确定性，重生成的新 CA 同名但密钥不同——仅比名字会把
    旧 CA 签的 leaf 误判仍有效。用公钥验签才是真判据（换机/CA 丢失重建都能自愈）。"""
    try:
        from cryptography.hazmat.primitives.asymmetric import padding
        ca_cert.public_key().verify(
            leaf.signature, leaf.tbs_certificate_bytes,
            padding.PKCS1v15(), leaf.signature_hash_algorithm)
        return True
    except Exception:
        return False


# https 启动时**实际出示**的叶子 PEM 快照（内存）。运行中重写证书文件不改变已出示的叶子，
# 故一致性判断必须以“出示的那张”为准，而非磁盘上可能被重生成的新文件。
_SERVED_LEAF_PEM = None


def _cert_chain_state() -> dict:
    """证书链一致性：https 实际出示的叶子 是否由 当前 /cert.pem 那张 CA 签发。
    返回 {ca_fp, served, chain_ok}；chain_ok=None=无 https 叶子(纯 http)或无法判定。
    痛点固化：装了 CA 却仍告警，多因“叶子由旧 CA 签、CA 已被换/重建”——此处一处算清，
    供扫码页红灯 / 交付门禁 / 手机端共用，把哑谜变成可见信号。"""
    out = {"ca_fp": "", "served": False, "chain_ok": None}
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        import hashlib
        if not CA_CERT.exists():
            return out
        ca = x509.load_pem_x509_certificate(CA_CERT.read_bytes())
        out["ca_fp"] = hashlib.sha256(ca.public_bytes(serialization.Encoding.DER)).hexdigest()[:8]
        leaf_pem = _SERVED_LEAF_PEM
        if not leaf_pem:
            lf = BASE / "_monitor_cert.pem"     # 未捕获快照(未经 __main__)时退回磁盘叶子
            leaf_pem = lf.read_bytes() if lf.exists() else None
        if not leaf_pem:
            return out
        out["served"] = True
        out["chain_ok"] = _leaf_signed_by(x509.load_pem_x509_certificate(leaf_pem), ca)
    except Exception:
        pass
    return out


def _cert_covers_ip(ip: str):
    """运行中的自签证书 SAN 是否含当前 IP。True/False/None(无证书或读失败)。
    IP 变更后若为 False，说明 https 证书需更新(重启中继生效;不影响 http 监听)。"""
    cf = BASE / "_monitor_cert.pem"
    try:
        from cryptography import x509
        if not cf.exists():
            return None
        c = x509.load_pem_x509_certificate(cf.read_bytes())
        san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        ips = [str(a) for a in san.get_values_for_type(x509.IPAddress)]
        return ip in ips
    except Exception:
        return None


def _firewall_rule_ok(port: int):
    """检查入站防火墙规则 'MonitorRelay <port>' 是否存在(best-effort)。True/False/None(未知)。"""
    try:
        out = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name=MonitorRelay {port}"],
            capture_output=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        raw = (out.stdout or b"") + (out.stderr or b"")
        txt = raw.decode("gbk", "ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
        if ("No rules match" in txt) or ("没有" in txt) or ("不存在" in txt):
            return False
        return (f"MonitorRelay {port}" in txt) or ("Allow" in txt) or ("允许" in txt)
    except Exception:
        return None


def _network_check(reason: str = "") -> dict:
    """网络环境检查:当前 IP/子网、是否可被局域网访问、URL、证书覆盖、防火墙端口。"""
    ip = _lan_ip()
    return {"ip": ip, "subnet": _subnet_of(ip),
            "lan_ok": bool(ip and not ip.startswith("127.")),
            "bound_all": True,                       # 服务绑 0.0.0.0 → 新 IP 自动可达，无需重启
            "http_url": f"http://{ip}:{PORT}/",
            "https_url": f"https://{ip}:{HTTPS_PORT}/",
            "show_url": f"http://{ip}:{PORT}/show",
            "cert_ip_ok": _cert_covers_ip(ip),
            "cert_chain_ok": _cert_chain_state()["chain_ok"],
            "fw_7878": _firewall_rule_ok(PORT),
            "fw_7879": _firewall_rule_ok(HTTPS_PORT),
            "reason": reason, "ts": time.time()}


def _apply_netcheck(reason: str, regen_cert_if_needed: bool = True) -> dict:
    """跑一次网络检查并写入 _net_state；按需为新 IP 重生成证书文件。返回 checks。"""
    ip = _lan_ip()
    if regen_cert_if_needed and ip and not ip.startswith("127.") and _cert_covers_ip(ip) is not True:
        try:
            _gen_cert(BASE / "_monitor_cert.pem", BASE / "_monitor_key.pem", ip)
        except Exception as e:
            logger.warning(f"为新 IP 重生成证书失败: {e}")
    checks = _network_check(reason)
    with _net_lock:
        _net_state["ip"] = ip
        _net_state["subnet"] = checks["subnet"]
        _net_state["checked_at"] = checks["ts"]
        _net_state["cert_ip_ok"] = checks["cert_ip_ok"]
        _net_state["checks"] = checks
    return checks


def _ip_watch():
    """周期检测 LAN IP 变化(换 WiFi/局域网)。变化即:刷新二维码版本→重生成证书→重跑网络检查→显著记录。
    服务绑 0.0.0.0，故 http/https 无需重启即可服务新 IP(仅自签证书 SAN 需更新,重启中继才换新证书)。"""
    last = None
    while not _stop.is_set():
        try:
            ip = _lan_ip()
        except Exception:
            ip = ""
        if ip and ip != last:
            first = last is None
            if not first:
                with _net_lock:
                    _net_state["qr_gen"] += 1
            checks = _apply_netcheck("startup" if first else "ip_changed")
            if first:
                logger.info(f"网络环境检查: IP={ip} 子网={checks['subnet']} "
                            f"防火墙7878={checks['fw_7878']} 7879={checks['fw_7879']} "
                            f"证书覆盖={checks['cert_ip_ok']}")
            else:
                with _net_lock:
                    gen = _net_state["qr_gen"]
                logger.warning(f"⚠ 检测到 IP 变化: {last} → {ip}。已刷新二维码(gen={gen})并重生成证书。"
                               f"手机请确认连同一 WiFi 后重扫扫码页; 防火墙7878={checks['fw_7878']} 7879={checks['fw_7879']}")
            last = ip
        _stop.wait(5.0)


_scan_lock = threading.Lock()
# P1-F 定向电平探针缓存：同一设备 ~1s 内复用一次采样，避免高频轮询反复开环回。
_probe_cache = {"ts": 0.0, "key": "", "data": {"ok": False, "dev": "", "rms": 0.0, "peak": 0.0}}


def _probe_one(name_sub: str, dur: float = 0.4) -> dict:
    """对名字含 name_sub 的输出设备做一次短环回，测 rms/peak(广播馈线=CABLE 的实时电平)。
    被动 WASAPI 环回，不影响该设备正在播放的音频；找不到设备/失败返回 ok=False(调用方据此不下结论)。"""
    import soundcard as sc
    try:
        from soundcard.mediafoundation import SoundcardRuntimeWarning
        warnings.filterwarnings("ignore", category=SoundcardRuntimeWarning)
    except Exception:
        pass
    nlow = (name_sub or "").strip().lower()
    if not nlow:
        return {"ok": False, "dev": "", "rms": 0.0, "peak": 0.0, "detail": "empty dev"}
    target = None
    for s in sc.all_speakers():
        if nlow in s.name.lower():
            target = s
            break
    if target is None:
        return {"ok": False, "dev": "", "rms": 0.0, "peak": 0.0, "detail": "device not found"}
    mic = sc.get_microphone(target.name, include_loopback=True)
    with mic.recorder(samplerate=SR, channels=2, blocksize=int(SR * dur)) as rec:
        data = rec.record(numframes=int(SR * dur))
    mono = data.mean(axis=1) if data.ndim > 1 else data
    peak = float(np.abs(mono).max()) if len(mono) else 0.0
    rms = float(np.sqrt(np.mean(mono * mono))) if len(mono) else 0.0
    return {"ok": True, "dev": target.name, "rms": round(rms, 5), "peak": round(peak, 5)}


def _scan_devices(dur: float = 0.35) -> list[dict]:
    """逐个输出设备做短环回，测峰值/电平 → 按响度排序(找"对方声在哪个设备")。"""
    import soundcard as sc
    results = []
    for s in sc.all_speakers():
        try:
            mic = sc.get_microphone(s.name, include_loopback=True)
            with mic.recorder(samplerate=SR, channels=2, blocksize=int(SR * dur)) as rec:
                data = rec.record(numframes=int(SR * dur))
            mono = data.mean(axis=1) if data.ndim > 1 else data
            peak = float(np.abs(mono).max()) if len(mono) else 0.0
            rms = float(np.sqrt(np.mean(mono * mono)) + 1e-12) if len(mono) else 1e-12
            results.append({"name": s.name, "peak": round(peak, 5),
                            "dbfs": round(20 * float(np.log10(rms)), 1)})
        except Exception as e:
            results.append({"name": s.name, "peak": 0.0, "dbfs": -120.0, "err": str(e)[:60]})
    results.sort(key=lambda x: x["peak"], reverse=True)
    return results


def _pick_speaker(sc, name: str | None):
    if not name:
        return sc.default_speaker()
    nlow = name.lower()
    for s in sc.all_speakers():
        if nlow in s.name.lower():
            return s
    return sc.default_speaker()


def _capture_supervisor():
    """常驻：按当前 _dev_name 打开 WASAPI 环回，逐帧 PCM16 广播；设备变更/出错自动重开。"""
    try:
        import soundcard as sc
        try:                                   # 按类别彻底静音 soundcard 的空闲不连续告警
            from soundcard.mediafoundation import SoundcardRuntimeWarning
            warnings.filterwarnings("ignore", category=SoundcardRuntimeWarning)
        except Exception:
            pass
    except Exception as e:
        _cap_status.update(ok=False, err=f"soundcard 不可用: {e}")
        logger.error(_cap_status["err"])
        return
    while not _stop.is_set():
        my_gen = _gen
        name = _dev_name
        try:
            spk = _pick_speaker(sc, name)
            mic = sc.get_microphone(spk.name, include_loopback=True)
            _cap_status.update(ok=True, dev=spk.name, err="")
            logger.info(f"环回采集启动: '{spk.name}' sr={SR} frame={FRAME}")
            with mic.recorder(samplerate=SR, channels=2, blocksize=FRAME) as rec:
                while not _stop.is_set() and my_gen == _gen:
                    data = rec.record(numframes=FRAME)
                    if data is None or not len(data):
                        continue
                    mono = data.mean(axis=1) if data.ndim > 1 else data
                    i16 = (np.clip(mono, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
                    _cap_status["frames"] += 1
                    if _cap_status["frames"] % 50 == 0:   # 每~1s 更新一次电平(诊断用)
                        _cap_status["rms"] = float(np.sqrt(np.mean(mono * mono)) + 1e-9)
                    _broadcast(i16)
        except Exception as e:
            _cap_status.update(ok=False, err=str(e))
            logger.warning(f"环回采集异常，1s 后重试: {e}")
            time.sleep(1.0)


def _ensure_bg():
    """http/https 两个 server 都会触发 startup，这里保证后台线程只起一次。"""
    global _bg_started
    with _bg_lock:
        if _bg_started:
            return
        _bg_started = True
    threading.Thread(target=_capture_supervisor, daemon=True).start()
    threading.Thread(target=_mic_player_supervisor, daemon=True).start()
    threading.Thread(target=_ip_watch, daemon=True).start()   # IP 变化(换WiFi)→自动复检+刷新二维码
    ip = _lan_ip()
    url = f"http://{ip}:{PORT}/"
    logger.info(f"MonitorRelay 启动: http://0.0.0.0:{PORT}/  (字幕源 {INTERP_URL})")
    logger.info(f"📱 监听(放音+字幕): {url}   PC 扫码页 http://127.0.0.1:{PORT}/show")
    logger.info(f"🎤 对讲(手机当麦,需https): https://{ip}:{HTTPS_PORT}/")
    try:                                   # 控制台直接打印可扫的二维码
        import qrcode
        q = qrcode.QRCode(border=1)
        q.add_data(url)
        q.make()
        buf = io.StringIO()
        q.print_ascii(out=buf, invert=True)
        for line in buf.getvalue().splitlines():
            logger.info(line)
    except Exception:
        pass


@app.on_event("startup")
async def _on_start():
    _ensure_bg()


@app.on_event("shutdown")
async def _on_stop():
    _stop.set()


@app.get("/health")
def health():
    return {"ok": True, "service": "monitor_relay", "capture": _cap_status,
            "clients": len(_subs), "sr": SR, "cam": _cam_status}


@app.get("/devices")
def devices():
    try:
        import soundcard as sc
        spks = sc.all_speakers()
        default = sc.default_speaker().name
        return {"speakers": [{"name": s.name, "default": s.name == default} for s in spks],
                "current": _dev_name, "capture": _cap_status}
    except Exception as e:
        return JSONResponse({"error": str(e), "speakers": []}, status_code=500)


@app.post("/select")
async def select(dev: str = ""):
    """切换要环回的输出设备(按名字子串)。空=默认扬声器。"""
    global _dev_name, _gen
    _dev_name = dev or None
    _gen += 1                       # 让采集线程重开到新设备
    return {"ok": True, "dev": _dev_name}


@app.get("/subs")
async def subs(since: int = 0):
    """把通译 /events(127.0.0.1) 流式代理成同源 SSE，供手机页订阅字幕。"""
    async def gen():
        import httpx
        url = f"{INTERP_URL}/events?since={since}"
        try:
            async with httpx.AsyncClient(timeout=None) as cli:
                async with cli.stream("GET", url) as r:
                    async for chunk in r.aiter_raw():
                        yield chunk
        except Exception as e:
            yield f": interpreter unreachable ({e})\n\n".encode()
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.websocket("/ws/monitor")
async def ws_monitor(ws: WebSocket):
    if _MONITOR_AUTH and service_auth and not service_auth.ws_authorized(ws):   # 加固开启后：非回环/白名单/令牌一律拒握手
        await ws.close(code=1008); return
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=QCAP)
    entry = (q, asyncio.get_running_loop())
    with _subs_lock:
        _subs.add(entry)
    try:
        await ws.send_json({"sr": SR, "ch": 1, "fmt": "pcm16"})
        while True:
            data = await q.get()
            await ws.send_bytes(data)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.info(f"WS 关闭: {e}")
    finally:
        with _subs_lock:
            _subs.discard(entry)


@app.websocket("/ws/mic")
async def ws_mic(ws: WebSocket):
    """手机麦上行：首帧可发 {"sr":采样率} 握手，之后连续发 PCM16 单声道二进制帧。"""
    if _MONITOR_AUTH and service_auth and not service_auth.ws_authorized(ws):
        await ws.close(code=1008); return
    await ws.accept()
    src_sr = SR
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            b = msg.get("bytes")
            if b is not None:
                _mic_ingest(b, src_sr)
                continue
            t = msg.get("text")
            if t:
                try:
                    j = json.loads(t)
                    if "sr" in j:
                        src_sr = int(j["sr"])
                except Exception:
                    pass
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.info(f"mic WS 关闭: {e}")


def _cam_store_bgr(img: np.ndarray, source: str):
    """写入最新摄像头帧(线程安全)并更新观测计数。"""
    global _cam_frame, _cam_fps_t0, _cam_fps_n
    with _cam_lock:
        _cam_frame = img
        _cam_status["frames"] += 1
        _cam_status["w"] = int(img.shape[1])
        _cam_status["h"] = int(img.shape[0])
        _cam_status["source"] = source
        _cam_status["connected"] = True
        _cam_fps_n += 1
        now = time.time()
        if _cam_fps_t0 <= 0:
            _cam_fps_t0 = now
        dt = now - _cam_fps_t0
        if dt >= 1.0:
            _cam_status["fps"] = round(_cam_fps_n / dt, 1)
            _cam_fps_t0 = now
            _cam_fps_n = 0


async def _consume_cam_track(track):
    """aiortc 入站视频轨 → BGR 帧缓冲。"""
    try:
        while True:
            frame = await track.recv()
            img = frame.to_ndarray(format="bgr24")
            _cam_store_bgr(img, "webrtc")
    except Exception as e:
        logger.info(f"cam WebRTC 轨结束: {e}")
    finally:
        with _cam_lock:
            if not _cam_pcs:
                _cam_status["connected"] = False
                _cam_status["source"] = ""


@app.post("/webrtc/cam/offer")
async def webrtc_cam_offer(request: Request):
    """手机浏览器 sendonly 视频 offer → answer；收到轨后写入 /cam.mjpeg 缓冲。"""
    from aiortc import RTCPeerConnection, RTCSessionDescription
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
    pc = RTCPeerConnection()
    _cam_pcs.add(pc)

    @pc.on("connectionstatechange")
    async def on_state():
        logger.info(f"cam WebRTC 状态: {pc.connectionState}")
        if pc.connectionState in ("failed", "closed", "disconnected"):
            try:
                await pc.close()
            except Exception:
                pass
            _cam_pcs.discard(pc)
            _cam_status["peers"] = len(_cam_pcs)
            with _cam_lock:
                if not _cam_pcs:
                    _cam_status["connected"] = False

    @pc.on("track")
    def on_track(track):
        if track.kind == "video":
            asyncio.create_task(_consume_cam_track(track))

    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    _cam_status["peers"] = len(_cam_pcs)
    logger.info(f"cam WebRTC 新对端，总连接 {len(_cam_pcs)}")
    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}


@app.websocket("/ws/cam")
async def ws_cam(ws: WebSocket):
    """JPEG 帧上行兜底(WebRTC ICE 失败时)。每帧=完整 JPEG 二进制。"""
    if _MONITOR_AUTH and service_auth and not service_auth.ws_authorized(ws):
        await ws.close(code=1008); return
    await ws.accept()
    import cv2
    try:
        while True:
            data = await ws.receive_bytes()
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                _cam_store_bgr(img, "ws")
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.info(f"cam WS 关闭: {e}")
    finally:
        with _cam_lock:
            if _cam_status.get("source") == "ws":
                _cam_status["connected"] = False
                _cam_status["source"] = ""


@app.get("/cam/status")
def cam_status():
    ip = _lan_ip()
    local = f"http://127.0.0.1:{PORT}/cam.mjpeg"
    return {"ok": True, "status": dict(_cam_status),
            "url_local": local,
            "url_lan": f"http://{ip}:{PORT}/cam.mjpeg",
            "realtime_hint": f"realtime_stream --source {local}"}


@app.get("/cam/snapshot")
def cam_snapshot():
    from fastapi.responses import Response
    import cv2
    with _cam_lock:
        frm = _cam_frame.copy() if _cam_frame is not None else None
    if frm is None:
        return JSONResponse({"ok": False, "detail": "暂无手机摄像头画面"}, status_code=503)
    ok, buf = cv2.imencode(".jpg", frm, [int(cv2.IMWRITE_JPEG_QUALITY), CAM_JPEG_Q])
    if not ok:
        return JSONResponse({"ok": False, "detail": "编码失败"}, status_code=500)
    return Response(content=buf.tobytes(), media_type="image/jpeg")


@app.get("/cam.mjpeg")
async def cam_mjpeg():
    """MJPEG 流 → realtime_stream/OpenCV 可直接当 HTTP 摄像头源读取。"""
    import cv2
    boundary = b"--mjpegframe"
    interval = 1.0 / max(CAM_FPS, 1)

    async def gen():
        while True:
            with _cam_lock:
                frm = _cam_frame.copy() if _cam_frame is not None else None
            if frm is not None:
                ok, jpeg = cv2.imencode(".jpg", frm, [int(cv2.IMWRITE_JPEG_QUALITY), CAM_JPEG_Q])
                if ok:
                    data = jpeg.tobytes()
                    yield boundary + b"\r\n"
                    yield b"Content-Type: image/jpeg\r\n"
                    yield f"Content-Length: {len(data)}\r\n\r\n".encode()
                    yield data + b"\r\n"
            await asyncio.sleep(interval)

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=mjpegframe",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/mic/devices")
def mic_devices():
    """可作为"对讲注入口"的输出设备(选一个空闲虚拟声卡，解释器再把它选成本人麦)。"""
    try:
        import soundcard as sc
        spks = sc.all_speakers()
        return {"speakers": [{"name": s.name} for s in spks],
                "current": _mic_target, "status": _mic_status}
    except Exception as e:
        return JSONResponse({"error": str(e), "speakers": []}, status_code=500)


_HP_PREF = {"Windows WASAPI": 0, "MME": 1, "Windows DirectSound": 2, "Windows WDM-KS": 3}


def _pair_tokens(playback: str) -> list[str]:
    core = playback.split("(")[0].lower()
    return [t for t in re.findall(r"[a-z0-9]+", core)
            if t not in ("input", "in", "vb", "audio", "virtual")]


def _best_dev(cands: list[dict]):
    """同名多 hostapi 时，优先 WASAPI(与解释器 _find_device 取向一致)，再短名。"""
    cands = sorted(cands, key=lambda it: (_HP_PREF.get(it.get("hostapi", ""), 9), len(it["name"])))
    return cands[0] if cands else None


async def _pair_recording(playback: str) -> dict:
    """给定注入用的播放口 → 解释器端应选作"本人麦"的录音口。
    · VB-Cable：1:1 确定配对(CABLE Input↔CABLE Output)，可自动给出精确 index。
    · Voicemeeter：走内部 A/B 总线路由矩阵，名字无法确定 → 返回可选 B 总线 + 指引(不瞎猜)。"""
    pl = playback or ""
    low = pl.lower()
    out = {"playback": pl, "index": None, "name": "", "hostapi": "", "exact": False, "kind": ""}
    inputs = []
    try:
        import httpx
        async with httpx.AsyncClient(timeout=3) as cli:
            inputs = (await cli.get(f"{INTERP_URL}/devices")).json().get("inputs", [])
    except Exception as e:
        out["err"] = str(e)[:80]

    if "cable" in low and "16ch" not in low:             # VB-Cable：确定 1:1
        out["kind"] = "cable"
        b = _best_dev([it for it in inputs if "cable output" in it["name"].lower()])
        if b:
            out.update(index=b["index"], name=b["name"], hostapi=b.get("hostapi", ""), exact=True)
        else:
            out["guess_name"] = "CABLE Output"
        return out

    if "voicemeeter" in low:                             # Voicemeeter：路由矩阵，给候选不瞎配
        out["kind"] = "voicemeeter"
        bbus = [it for it in inputs if re.search(r"voicemeeter out b\d", it["name"].lower())]
        bbus = sorted(bbus, key=lambda it: (_HP_PREF.get(it.get("hostapi", ""), 9), it["name"]))
        seen, uniq = set(), []
        for it in bbus:                                  # 同名多 hostapi 去重，留首选
            key = it["name"].split("(")[0].strip()
            if key not in seen:
                seen.add(key)
                uniq.append({"index": it["index"], "name": it["name"]})
        out["bbus"] = uniq
        out["guess_name"] = "Voicemeeter Out B1/B2/B3（需在 Voicemeeter 把该输入路由到对应 B 总线）"
        return out

    toks = _pair_tokens(pl)                              # 通用：严格全词命中才算精确
    cands = [it for it in inputs
             if all(t in it["name"].lower() for t in toks)
             and ("output" in it["name"].lower() or re.search(r"\bout\b", it["name"].lower()))]
    b = _best_dev(cands)
    if b:
        out.update(index=b["index"], name=b["name"], hostapi=b.get("hostapi", ""), exact=True)
    else:
        out["guess_name"] = pl.split("(")[0].replace("Input", "Output").strip()
    return out


async def _interp_devices() -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=3) as cli:
        return (await cli.get(f"{INTERP_URL}/devices")).json()


async def _cloned_voice_bus(devs: dict | None = None) -> str:
    """解释器默认克隆音输出口（通常是 CABLE Input）。"""
    try:
        devs = devs or await _interp_devices()
        ci = (devs.get("defaults") or {}).get("cable")
        for it in devs.get("outputs", []):
            if it["index"] == ci:
                return it["name"]
    except Exception:
        pass
    return ""


async def _cable_plan() -> dict:
    """分线向导：检测可用 VB-Cable 线路，判断是否与克隆音冲突。"""
    import soundcard as sc
    cloned = ""
    devs = {}
    try:
        devs = await _interp_devices()
        cloned = await _cloned_voice_bus(devs)
    except Exception as e:
        devs = {}
        cloned = ""
    try:
        spks = [s.name for s in sc.all_speakers()]
    except Exception as e:
        return {"ok": False, "detail": f"枚举输出设备失败: {e}"}

    lanes = []
    for n in spks:
        low = n.lower()
        if "cable" not in low:
            continue
        kind = "16ch" if "16ch" in low else "standard"
        pair = await _pair_recording(n)
        occupied = bool(cloned) and (n.lower() == cloned.lower() or ("cable" in cloned.lower() and kind == "standard"))
        stable = kind == "standard" and bool(pair.get("exact"))
        lanes.append({
            "playback": n,
            "kind": kind,
            "pair": pair,
            "occupied_by_clone": occupied,
            "stable": stable,
            "usable_for_phone_mic": stable and not occupied,
            "note": ("标准 VB-Cable，适合手机麦精确配对"
                     if kind == "standard" else
                     "16ch/Point 端点，通常走 WDM-KS；可实验，不建议作为默认解释器麦")
        })

    usable = next((x for x in lanes if x["usable_for_phone_mic"]), None)
    standard = [x for x in lanes if x["kind"] == "standard"]
    if usable:
        plan = "use_free_standard_cable"
        summary = f"可用独立线路：{usable['playback']} → {usable['pair'].get('name') or '对应 Output'}"
    elif standard and "cable" in cloned.lower():
        plan = "need_second_cable_or_voicemeeter"
        summary = "标准 VB-Cable 已被克隆音占用；手机麦不要共用它。建议安装第二条标准 VB-Cable，或临时走 Voicemeeter B 总线。"
    elif standard:
        plan = "standard_available_but_pair_unknown"
        summary = "检测到标准 VB-Cable，但未能确认稳定录音端；建议检查驱动或重启音频服务。"
    else:
        plan = "no_standard_cable"
        summary = "未检测到标准 VB-Cable；建议安装 VB-Cable 或 VB-Cable A+B，用其中一条给克隆音，另一条给手机麦。"
    return {"ok": True, "cloned_voice_bus": cloned, "lanes": lanes,
            "recommended": usable, "plan": plan, "summary": summary}


@app.post("/mic/select")
async def mic_select(dev: str = ""):
    global _mic_target, _mic_gen
    _mic_target = dev or None
    _mic_gen += 1
    pair = await _pair_recording(dev) if dev else None
    return {"ok": True, "dev": _mic_target, "pair": pair}


@app.get("/mic/pair")
async def mic_pair(dev: str = ""):
    if not dev:
        return {"ok": False, "detail": "缺少 dev"}
    return {"ok": True, "pair": await _pair_recording(dev)}


@app.get("/mic/recommend")
async def mic_recommend():
    """一键判定：避开克隆音占用的总线，推荐干净的手机麦注入口 + 配对麦。"""
    import soundcard as sc
    try:
        plan = await _cable_plan()
    except Exception:
        plan = {}
    cloned = (plan or {}).get("cloned_voice_bus", "")
    try:
        spks = [s.name for s in sc.all_speakers()]
    except Exception as e:
        return {"ok": False, "detail": f"枚举输出设备失败: {e}"}
    free = (plan or {}).get("recommended") or {}
    cables = [free["playback"]] if free.get("playback") else [
        n for n in spks if "cable" in n.lower() and "16ch" not in n.lower()]
    cloned_is_cable = "cable" in cloned.lower()
    chosen, needs_route = None, False
    if cables and not cloned_is_cable:           # 有空闲 CABLE → 最省心(可精确配对)
        chosen = cables[0]
    else:                                        # CABLE 被克隆音占用 → 用 Voicemeeter AUX(需一次路由)
        aux = [n for n in spks if "aux" in n.lower()]
        v3 = [n for n in spks if "vaio3" in n.lower()]
        vm = [n for n in spks if "voicemeeter" in n.lower() and "out" not in n.lower()]
        cand = aux or v3 or vm or cables or spks
        chosen = cand[0] if cand else None
        needs_route = "voicemeeter" in (chosen or "").lower()
    pair = await _pair_recording(chosen) if chosen else None
    return {"ok": True, "cloned_voice_bus": cloned, "chosen": chosen,
            "needs_route": needs_route, "pair": pair, "cable_plan": plan}


@app.get("/mic/cable_plan")
async def mic_cable_plan():
    return await _cable_plan()


@app.post("/mic/align")
async def mic_align():
    """跟随解释器：读它当前『我的麦』，自动把注入口对齐到对应播放口(仅 CABLE 可名字反推)。"""
    import soundcard as sc
    try:
        import httpx
        async with httpx.AsyncClient(timeout=3) as cli:
            stt = (await cli.get(f"{INTERP_URL}/status")).json()
    except Exception as e:
        return {"ok": False, "detail": f"解释器不可达: {e}"}
    mic = (stt.get("mic_name") or "").strip()
    if not mic:
        return {"ok": False, "detail": "解释器未运行或未选『我的麦』"}
    low = mic.lower()
    try:
        spks = [s.name for s in sc.all_speakers()]
    except Exception as e:
        return {"ok": False, "detail": f"枚举输出设备失败: {e}"}
    global _mic_target, _mic_gen
    if "cable output" in low:                    # CABLE Output → CABLE Input(干净反推)
        tgt = next((n for n in spks if "cable input" in n.lower() and "16ch" not in n.lower()), None)
        if tgt:
            _mic_target = tgt
            _mic_gen += 1
            return {"ok": True, "aligned": True, "mic": mic, "target": tgt}
        return {"ok": False, "mic": mic, "detail": "找不到 CABLE Input 播放口"}
    if re.search(r"voicemeeter out b\d", low):
        return {"ok": False, "mic": mic,
                "detail": "解释器麦是 Voicemeeter B 总线：注入口须在 Voicemeeter 里把手机麦所在输入路由到该 B 总线(矩阵无法名字反推)"}
    return {"ok": False, "mic": mic,
            "detail": f"解释器麦『{mic}』不是可注入的虚拟录音口(疑似硬件麦)，无法注入手机麦"}


@app.get("/mic/level")
def mic_level():
    return {"ok": True, "status": _mic_status, "qdepth": _mic_q.qsize(), "taps": len(_mic_taps)}


@app.get("/mic/pcm")
async def mic_pcm(request: Request):
    """手机麦原始 PCM(int16 单声道 @ SR) 分块流，供解释器直连当"本人麦"(零中转，免 VB-Cable)。
    响应头携带采样率/格式;客户端断开即自动注销订阅(无泄漏)。"""
    q: queue.Queue = queue.Queue(maxsize=50)
    with _mic_taps_lock:
        _mic_taps.add(q)
    logger.info(f"麦克风出口订阅+1(直连): taps={len(_mic_taps)}")

    silence = b"\x00" * (FRAME * 2)   # 20ms 静音(int16 mono)：空闲保活，对 VAD 无害(等同真实麦常流)
    async def gen():
        try:
            while not _stop.is_set():
                if await request.is_disconnected():
                    break
                try:
                    chunk = await asyncio.to_thread(q.get, True, 0.5)
                except queue.Empty:
                    yield silence    # 保活并让消费端及时响应停止
                    continue
                if chunk:
                    yield chunk
        finally:
            with _mic_taps_lock:
                _mic_taps.discard(q)
            logger.info(f"麦克风出口订阅-1(直连断开): taps={len(_mic_taps)}")

    return StreamingResponse(gen(), media_type="application/octet-stream",
                             headers={"X-Sample-Rate": str(SR), "X-Channels": "1",
                                      "X-Format": "s16le", "Cache-Control": "no-store"})


@app.get("/info")
def info():
    ip = _lan_ip()
    with _net_lock:
        gen = _net_state.get("qr_gen", 0)
        cert_ok = _net_state.get("cert_ip_ok")
    cc = _cert_chain_state()
    return {"ip": ip, "port": PORT, "https_port": HTTPS_PORT,
            "url": f"http://{ip}:{PORT}/", "https_url": f"https://{ip}:{HTTPS_PORT}/",
            "cam_url_local": f"http://127.0.0.1:{PORT}/cam.mjpeg",
            "cam_url_lan": f"http://{ip}:{PORT}/cam.mjpeg",
            "ip_gen": gen, "cert_ip_ok": cert_ok,
            "cert_chain_ok": cc["chain_ok"], "ca_fp": cc["ca_fp"]}   # ip_gen 变=换了网络,页面据此自动刷新二维码


@app.get("/netcheck")
def netcheck_get():
    """读取最近一次网络环境检查(IP/子网/URL/证书/防火墙)。供扫码页/主控台展示。"""
    with _net_lock:
        st = dict(_net_state)
    if not st.get("checks"):
        st["checks"] = _apply_netcheck("on_demand")
        with _net_lock:
            st["ip"] = _net_state["ip"]; st["qr_gen"] = _net_state["qr_gen"]
            st["cert_ip_ok"] = _net_state["cert_ip_ok"]; st["checked_at"] = _net_state["checked_at"]
    return st


@app.post("/netcheck")
def netcheck_post():
    """立即重跑网络环境检查(手动复检按钮用)。"""
    checks = _apply_netcheck("manual")
    with _net_lock:
        st = dict(_net_state)
    st["checks"] = checks
    return st


# ── 同传(通译)会话代理：让手机端不碰电脑也能启停翻译 ──────────────────────
@app.get("/interp/status")
async def interp_status():
    """同传会话状态，供手机端「开始同传」按钮/状态灯轮询。"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=2.5) as cli:
            s = (await cli.get(f"{INTERP_URL}/status")).json()
        rb = s.get("readback") or {}
        return {"ok": True, "reachable": True,
                "running": bool(s.get("running")),
                "stream_on": s.get("stream_on"),
                "live_mode": bool(s.get("live_mode")),
                # P0g 无音色守卫:False=当前角色无音色样本,配音全程跳过(手机状态灯亮警示)
                "voice_ok": s.get("voice_ok"), "profile": s.get("profile") or "",
                "mic_name": s.get("mic_name") or "",
                "loop_name": s.get("loop_name") or "",
                "cap_a_err": s.get("cap_a_err"), "cap_b_err": s.get("cap_b_err"),
                # P3-2 手机通话控制条所需状态(急停/朗读随 PC 侧变化同步亮灯)
                "muted": bool(s.get("muted")),
                "readback_on": bool(rb.get("on")), "readback_ref": bool(rb.get("ref_locked")),
                # P8 场景方案/双工/冲突(手机场景卡同步)
                "audio_profile": s.get("audio_profile"), "half_duplex": s.get("half_duplex"),
                "coupling": s.get("coupling"), "conflicts": s.get("conflicts") or []}
    except Exception as e:
        return {"ok": False, "reachable": False, "running": False, "detail": str(e)}


@app.post("/interp/start")
async def interp_start(request: Request):
    """一键启动同传：默认手机麦无线直连(/mic/pcm)+立体声混音抓对方声+Hub激活角色。
    可选 body: {use_phone_mic:bool=true, live_mode:bool=false, profile:str=""}"""
    import httpx
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}
    use_phone_mic = bool(body.get("use_phone_mic", True))
    live_mode = bool(body.get("live_mode", False))
    try:
        async with httpx.AsyncClient(timeout=4) as cli:
            devs = (await cli.get(f"{INTERP_URL}/devices")).json()
    except Exception as e:
        return {"ok": False, "detail": f"取同传设备失败: {e}"}
    defs = devs.get("defaults", {}) or {}
    stereo = devs.get("stereo_mix")
    if stereo is not None:
        loop_idx, loop_out = int(stereo), False
    elif defs.get("loopback") is not None:
        loop_idx, loop_out = int(defs["loopback"]), bool(defs.get("loopback_is_output"))
    elif devs.get("loopback_ok"):
        # 本机没启用「立体声混音」但支持 WASAPI 环回 → 抓默认输出的对方声(与 PC 一键通话同策略)。
        # loopback_is_output=True 让采集端走 soundcard 环回回退到默认扬声器,index=-1 即为该哨兵。
        # 修复:此前这里回落成 (-1, False) → 对方声采集直接失败(只出自己字幕),且 -1 与手机麦哨兵
        # 相等还会误报"我的麦=对方声来源同设备"(电脑198实测 2026-07-14)。
        loop_idx, loop_out = -1, True
    else:
        loop_idx, loop_out = -1, False
    start = {
        "mic_index": -1 if use_phone_mic else int(defs.get("mic") if defs.get("mic") is not None else 0),
        "mic_net_url": f"http://127.0.0.1:{PORT}/mic/pcm" if use_phone_mic else "",
        "cable_index": int(defs.get("cable")) if defs.get("cable") is not None else -1,
        "loopback_index": loop_idx,
        "loopback_is_output": loop_out,
        "profile": str(body.get("profile", "") or ""),
        "mode": "local",
        "live_mode": live_mode,
    }
    try:
        async with httpx.AsyncClient(timeout=25) as cli:
            r = (await cli.post(f"{INTERP_URL}/start", json=start)).json()
        return {"ok": bool(r.get("ok")), "sent": start, "result": r}
    except Exception as e:
        return {"ok": False, "detail": f"启动同传失败: {e}", "sent": start}


@app.post("/interp/stop")
async def interp_stop():
    """停止同传会话。"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=8) as cli:
            r = (await cli.post(f"{INTERP_URL}/stop")).json()
        return {"ok": True, "result": r}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


# ── P3-2 手机端一键通话：代理通译的通话模式/急停/朗读/调参(通译绑 127.0.0.1,手机只能经本中继) ──
async def _interp_proxy_post(path: str, payload: dict = None, timeout: float = 12.0):
    """POST 透传到通译并原样回 JSON。通译不可达 → ok:False + 提示(手机端好懂)。"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=timeout) as cli:
            r = await cli.post(f"{INTERP_URL}{path}", json=(payload or {}))
        try:
            return r.json()
        except Exception:
            return {"ok": False, "detail": (r.text or "")[:200]}
    except Exception as e:
        return {"ok": False, "detail": f"通译服务不可达: {e}", "reachable": False}


@app.post("/interp/call/start")
async def interp_call_start(request: Request):
    """一键通话模式(PC 麦/CABLE/环回全自动)：默认麦切 CABLE→按名绑定→链路自检→开播。
    自检含测试音+子进程探测,最长可到 ~60s,超时给足。可选 body {profile,stream}。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _interp_proxy_post("/call_mode/start", body or {}, timeout=90.0)


@app.post("/interp/call/stop")
async def interp_call_stop():
    """收尾通话模式：停会话+系统默认麦还原为物理麦。"""
    return await _interp_proxy_post("/call_mode/stop", {}, timeout=30.0)


@app.get("/interp/call/status")
async def interp_call_status():
    import httpx
    try:
        async with httpx.AsyncClient(timeout=3) as cli:
            return (await cli.get(f"{INTERP_URL}/call_mode/status")).json()
    except Exception as e:
        return {"ready": False, "steps": [], "detail": str(e)}


@app.post("/interp/tts_engine_restart")
async def interp_tts_engine_restart(request: Request):
    """一键拉起配音引擎(透传通译 /tts/engine_restart)：手机上"无音色·仅字幕"多因引擎没起,
    点一下把 Fish/CosyVoice 等经主控台拉起来。引擎已在跑则幂等无害。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _interp_proxy_post("/tts/engine_restart",
                                    {"engine": str((body or {}).get("engine", "") or "")},
                                    timeout=120.0)


@app.post("/interp/panic")
async def interp_panic(request: Request):
    """急停/恢复：{on:true} 0.1s 内切断正在播的克隆音。说错话/误识别的保险丝。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _interp_proxy_post("/panic", {"on": bool((body or {}).get("on", True))}, timeout=6.0)


@app.post("/interp/readback")
async def interp_readback(request: Request):
    """对方译文朗读开关：{on:bool}。开=对方中文译文用 TA 的音色读到 PC 耳机。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _interp_proxy_post("/readback", {"on": bool((body or {}).get("on", False))}, timeout=6.0)


@app.get("/interp/tune")
async def interp_tune_get():
    import httpx
    try:
        async with httpx.AsyncClient(timeout=3) as cli:
            return (await cli.get(f"{INTERP_URL}/tune")).json()
    except Exception as e:
        return {"ok": False, "detail": str(e)}


@app.post("/interp/tune")
async def interp_tune_set(request: Request):
    """实战调参透传：手机拖滑杆同样即时生效(与 PC 页同一份持久化)。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _interp_proxy_post("/tune", body or {}, timeout=6.0)


@app.post("/interp/tune/reset")
async def interp_tune_reset():
    """调参一键回出厂(P4-3 手机滑杆配套)。"""
    return await _interp_proxy_post("/tune/reset", {}, timeout=6.0)


# ── P5-3 手机端语向切换：代理通译 /config/langs(通译绑 127.0.0.1) ──
@app.get("/interp/langs")
async def interp_langs_get():
    import httpx
    try:
        async with httpx.AsyncClient(timeout=6) as cli:
            return (await cli.get(f"{INTERP_URL}/config/langs")).json()
    except Exception as e:
        return {"ok": False, "detail": str(e)}


# ── P6-3 会话质量卡：结束通话后手机 10s 内弹出本场摘要(同传 /session/last 的同源代理) ──
@app.get("/interp/last_session")
async def interp_last_session():
    import httpx
    try:
        async with httpx.AsyncClient(timeout=6) as cli:
            return (await cli.get(f"{INTERP_URL}/session/last")).json()
    except Exception as e:
        return {"ok": False, "detail": str(e)}


@app.post("/interp/langs")
async def interp_langs_set(request: Request):
    """切语向透传：{src,dst}。通译侧会自动预热新语对全部翻译层(P4-1)。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _interp_proxy_post("/config/langs", body or {}, timeout=8.0)


# ── P8 设备方案中心代理：手机页场景卡与 PC 同源(通译绑 127.0.0.1,手机只能经本中继) ──
@app.get("/interp/audio_profile")
async def interp_audio_profile_get():
    import httpx
    try:
        async with httpx.AsyncClient(timeout=6) as cli:
            return (await cli.get(f"{INTERP_URL}/audio_profile")).json()
    except Exception as e:
        return {"ok": False, "detail": str(e)}


@app.post("/interp/audio_profile")
async def interp_audio_profile_set(request: Request):
    """切场景方案透传：{active:"pc|phone|headset"} 或 {name,patch}。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _interp_proxy_post("/audio_profile", body or {}, timeout=10.0)


@app.post("/interp/coupling_probe")
async def interp_coupling_probe():
    """声学耦合自检透传(播 2 声测试音,~4s)。"""
    return await _interp_proxy_post("/coupling_probe", {}, timeout=40.0)


# ── P5-1 告警直达手机：手机常在手上,比配群机器人 webhook 更直接的"真会叫"通道。 ──
#   数据源=alerts.py 的状态/历史文件(与 PC 托盘/webhook 同一事实源)；手机页轮询+振动+系统通知。
@app.get("/alerts")
def get_alerts():
    """活动告警(firing) + 近 24h 点状事件(质量守门/漂移等 notify)。手机页 10s 轮询。"""
    act = {}
    try:
        import alerts as _al
        act = _al.active_alerts()
    except Exception:
        pass
    events = []
    try:
        hist = BASE / "logs" / "alerts.jsonl"
        if hist.exists():
            with open(hist, "r", encoding="utf-8") as f:
                lines = f.readlines()[-120:]
            cutoff = time.time() - 24 * 3600
            for ln in lines:
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                if r.get("event") not in ("raise", "notify"):
                    continue
                try:
                    tt = time.mktime(time.strptime(r.get("ts", ""), "%Y-%m-%d %H:%M:%S"))
                except Exception:
                    continue
                if tt < cutoff:
                    continue
                events.append({"ts": r.get("ts"), "t": tt, "event": r.get("event"),
                               "level": r.get("level"), "title": r.get("title"),
                               "detail": (str(r.get("detail") or ""))[:200],
                               "source": r.get("source")})
    except Exception:
        pass
    return {"ok": True, "active": act, "events": events[-20:], "now": time.time()}


@app.post("/alerts/test")
def alerts_test():
    """手机告警通路自检：注入一条点状测试事件(同时走 PC 托盘/webhook 通道)，手机 10s 内应弹横幅。"""
    try:
        import alerts as _al
        _al.notify_event("测试告警(手机通路自检)", "这条来自手机页自检按钮——看到横幅即通路正常",
                         level="info", source="monitor_relay")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


@app.get("/qr.png")
def qr_png(target: str = "http", path: str = ""):
    from fastapi.responses import Response
    ip = _lan_ip()
    base = f"https://{ip}:{HTTPS_PORT}" if target == "https" else f"http://{ip}:{PORT}"
    p = path if path.startswith("/") else ("/" + path if path else "/")
    url = base + p
    try:
        import qrcode
        buf = io.BytesIO()
        qrcode.make(url).save(buf, format="PNG")
        return Response(content=buf.getvalue(), media_type="image/png")
    except Exception as e:
        return JSONResponse({"error": f"qrcode 不可用: {e}", "url": url}, status_code=500)


def _ca_der_b64() -> str:
    from cryptography.hazmat.primitives import serialization
    import base64 as _b64
    ca, _ = _ensure_ca()
    return _b64.b64encode(ca.public_bytes(serialization.Encoding.DER)).decode()


@app.get("/cert.pem")
def cert_pem():
    """下载本地根 CA(供 Android/桌面安装信任)。经 http(7878) 取，避免下载时又撞证书告警。"""
    from cryptography.hazmat.primitives import serialization
    from fastapi.responses import Response
    ca, _ = _ensure_ca()
    return Response(content=ca.public_bytes(serialization.Encoding.PEM),
                    media_type="application/x-x509-ca-cert",
                    headers={"Content-Disposition": 'attachment; filename="avatarhub-ca.crt"'})


@app.get("/cert.mobileconfig")
def cert_mobileconfig():
    """iOS 描述文件：内嵌本地根 CA(com.apple.security.root)，装一次 + 证书信任设置里打开即免告警。"""
    from cryptography import x509
    from fastapi.responses import Response
    import uuid as _uuid
    der_b64 = _ca_der_b64()
    ca = x509.load_pem_x509_certificate(CA_CERT.read_bytes())
    fp = format(ca.serial_number, "x")
    u1 = _uuid.uuid5(_uuid.NAMESPACE_DNS, "avatarhub-ca-payload-" + fp)
    u2 = _uuid.uuid5(_uuid.NAMESPACE_DNS, "avatarhub-ca-config-" + fp)
    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        ' <key>PayloadContent</key><array><dict>\n'
        '  <key>PayloadType</key><string>com.apple.security.root</string>\n'
        '  <key>PayloadVersion</key><integer>1</integer>\n'
        '  <key>PayloadIdentifier</key><string>com.avatarhub.monitor.ca</string>\n'
        f'  <key>PayloadUUID</key><string>{u1}</string>\n'
        '  <key>PayloadDisplayName</key><string>AvatarHub Local CA</string>\n'
        '  <key>PayloadCertificateFileName</key><string>avatarhub-ca.cer</string>\n'
        f'  <key>PayloadContent</key><data>{der_b64}</data>\n'
        ' </dict></array>\n'
        ' <key>PayloadDisplayName</key><string>AvatarHub 免证书告警</string>\n'
        ' <key>PayloadDescription</key><string>安装本地根证书，手机访问本机同传页不再提示"连接不是私密"。</string>\n'
        ' <key>PayloadIdentifier</key><string>com.avatarhub.monitor</string>\n'
        ' <key>PayloadType</key><string>Configuration</string>\n'
        f' <key>PayloadUUID</key><string>{u2}</string>\n'
        ' <key>PayloadVersion</key><integer>1</integer>\n'
        '</dict></plist>'
    )
    return Response(content=plist, media_type="application/x-apple-aspen-config",
                    headers={"Content-Disposition": 'attachment; filename="avatarhub-ca.mobileconfig"'})


@app.get("/cert", response_class=HTMLResponse)
def cert_page():
    """手机端证书安装引导页（走 http，无告警）：iOS 装描述文件、安卓装 CA，装完永久免对讲页告警。"""
    return _CERT_PAGE.replace("__HTTPS_PORT__", str(HTTPS_PORT))


@app.get("/show", response_class=HTMLResponse)
def show():
    """PC 浏览器打开 → 双二维码：监听(http)、对讲(https)，手机一扫直达(免手输 IP)。
    页面自轮询 /info：换 WiFi/IP 变化时自动换码、更新链接并提示重扫。"""
    ip = _lan_ip()
    with _net_lock:
        gen = _net_state.get("qr_gen", 0)
    return (_SHOW.replace("__HTTP__", f"http://{ip}:{PORT}/")
                 .replace("__HTTPS__", f"https://{ip}:{HTTPS_PORT}/")
                 .replace("__IP__", ip)
                 .replace("__GEN__", str(gen))
                 .replace("__PORT__", str(PORT)))


@app.post("/autopick")
async def autopick():
    """扫描所有输出设备的环回响度，自动选最响的(=对方声所在)。需对方此刻在出声。"""
    if not _scan_lock.acquire(blocking=False):
        return {"ok": False, "detail": "扫描进行中"}
    try:
        results = await asyncio.to_thread(_scan_devices)
    finally:
        _scan_lock.release()
    global _dev_name, _gen
    best = results[0] if results else None
    picked = None
    if best and best.get("peak", 0) >= 0.003:    # ~ -50dBFS 以上视为有真实信号
        _dev_name = best["name"]
        _gen += 1
        picked = best["name"]
    return {"ok": True, "picked": picked, "results": results}


@app.get("/probe/level")
async def probe_level(dev: str = "", dur: float = 0.4):
    """P1-F 定向电平探针：测某输出设备(按名子串匹配，如 'CABLE Input'=广播馈线)的环回电平。
    供 avatar_hub 判「直播端是否真有声」。~1s 内缓存复用；与 /autopick 共用 _scan_lock 不并发抢 soundcard；
    忙时/异常返回上次缓存值(never 抛错，避免拖累上游 /realtime/signal)。被动环回不影响该设备播放。"""
    now = time.time()
    key = (dev or "").strip().lower()
    if now - _probe_cache["ts"] < 1.0 and _probe_cache["key"] == key:
        return {"cached": True, **_probe_cache["data"]}
    if not _scan_lock.acquire(blocking=False):
        return {"busy": True, "cached": True, **_probe_cache["data"]}
    try:
        data = await asyncio.to_thread(_probe_one, dev, dur)
    except Exception as e:
        data = {"ok": False, "dev": "", "rms": 0.0, "peak": 0.0, "detail": str(e)[:80]}
    finally:
        _scan_lock.release()
    _probe_cache.update(ts=now, key=key, data=data)
    return data


@app.get("/", response_class=HTMLResponse)
@app.get("/listen", response_class=HTMLResponse)
def page():
    return _PAGE


_SHOW = r"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>扫码连接 · 通译手机端</title>
<style>body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;
justify-content:center;gap:10px;background:#080b10;color:#e5e9f5;padding:20px;
font:16px/1.5 -apple-system,"Microsoft YaHei",sans-serif}
.wrap{display:flex;gap:36px;flex-wrap:wrap;justify-content:center}
.col{display:flex;flex-direction:column;align-items:center;gap:10px}
.card{background:#fff;padding:16px;border-radius:18px}img{width:260px;height:260px;display:block}
.lab{font-size:18px;font-weight:800} .u{font-size:13px;color:#8b96b0}
.h{color:#8b96b0;font-size:14px;text-align:center}.acc{color:#a855f7}
.note{margin-top:8px;font-size:13px;color:#8b96b0;text-align:center;max-width:640px}
.note b{color:#c4b5fd}
.col.primary{order:1}.col.sec{order:2;opacity:.88}
.col.sec .card{padding:12px}.col.sec img{width:172px;height:172px}
.pick{font-size:12px;font-weight:800;padding:3px 11px;border-radius:999px}
.pick.p{background:rgba(168,85,247,.2);border:1px solid rgba(168,85,247,.55);color:#e9d5ff}
.pick.s{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.14);color:#8b96b0}
.intent{color:#c4b5fd;font-size:13px;text-align:center;max-width:232px;line-height:1.5}
.col.sec .intent{color:#8b96b0}.intent b{color:#e9d5ff}
.cert{margin:8px 16px 0;max-width:640px;border:1px solid rgba(251,191,36,.4);background:rgba(251,191,36,.09);
 border-radius:12px;padding:12px 14px;font-size:13px;line-height:1.65;color:#f3d38a}
.cert b{color:#fde68a}.cert .t{font-weight:800;color:#fcd34d;margin-bottom:5px}
.certinstall{display:flex;gap:12px;align-items:center;margin-top:9px;padding-top:9px;border-top:1px dashed rgba(251,191,36,.3)}
.card2{background:#fff;padding:8px;border-radius:12px;flex:none}.certinstall img{width:118px;height:118px;display:block}
.certsteps{font-size:12px;color:#f3d38a;line-height:1.6}.certsteps a{color:#9bb4ff}
#ip{font-size:14px;color:#c4b5fd;font-weight:700}
#banner{display:none;margin:4px 0;padding:8px 14px;border-radius:10px;background:rgba(168,85,247,.18);
 border:1px solid rgba(168,85,247,.5);color:#e9d5ff;font-size:14px;text-align:center}
#net{display:flex;gap:8px;flex-wrap:wrap;justify-content:center;font-size:12px;color:#8b96b0}
.chip{padding:3px 10px;border-radius:999px;border:1px solid rgba(255,255,255,.14);background:rgba(255,255,255,.05)}
.chip.ok{border-color:rgba(52,211,153,.5);color:#86efac}
.chip.bad{border-color:rgba(248,113,113,.5);color:#fca5a5}
.chip.warn{border-color:rgba(251,191,36,.5);color:#fcd34d}
#recheck{cursor:pointer;border-color:rgba(79,122,255,.5);color:#9bb4ff}</style>
</head><body>
<div class=h>手机扫码直达 · 按“你要做什么”选一个</div>
<div id=ip>本机 IP：__IP__</div>
<div id=banner></div>
<div class=wrap>
 <div class="col primary">
  <div class="pick p">① 做同传 · 推荐</div>
  <div class=lab class=acc>🎤 手机当麦克风 + 摄像头</div>
  <div class=card><img class=qr data-t=https src="/qr.png?target=https&v=__GEN__" alt="talk"></div>
  <div class=u id=uhttps>__HTTPS__</div>
  <div class=intent>你要<b>开口说话</b>、让对方听到你的克隆音，或出镜换脸 → 扫这个</div>
 </div>
 <div class="col sec">
  <div class="pick s">② 只旁听</div>
  <div class=lab>🎧 只听对方 + 看字幕</div>
  <div class=card><img class=qr data-t=http src="/qr.png?target=http&v=__GEN__" alt="listen"></div>
  <div class=u id=uhttp>__HTTP__</div>
  <div class=intent>你<b>不说话</b>，只想听对方声、看中英字幕 → 扫这个（免证书最省事）</div>
 </div>
</div>
<div class=cert>
 <div class=t>🔒 扫①后手机提示“连接不是私密 / 证书无效”？——正常，这是本机自签证书，两种处理任选：</div>
 <b>A · 直接继续（每台手机首次点一次，最省事）</b><br>
 · <b>安卓 Chrome</b>：点「高级」→「继续前往（不安全）」<br>
 · <b>iPhone Safari</b>：点「显示详细信息」→「访问此网站」<br>
 · <b>微信里打开</b>：点右上「···」→「在浏览器打开」（微信不给麦克风/摄像头权限）
 <div class=certinstall>
  <div class=card2><img src="/qr.png?target=http&path=/cert&v=__GEN__" alt="装证书"></div>
  <div class=certsteps><b>B · 一劳永逸免告警（可选，装一次）</b><br>
   手机扫左边小码 → 按 iPhone / 安卓指引装一次本机证书 → 之后对讲页<b>不再弹告警</b>。<br>
   <a href="http://__IP__:__PORT__/cert" target="_blank">或在此电脑打开安装说明 ↗</a></div>
 </div>
</div>
<div id=net></div>
<div class=note>📷 摄像头：扫①进入后点 <b>开摄像头</b> → PC 换脸源设为 <b>http://127.0.0.1:__PORT__/cam.mjpeg</b></div>
<div class=note>🔌 手机连不上？先确认手机与电脑连同一 WiFi；仍不行则以管理员运行 <b>_allow_firewall_7878.bat</b> 放行 7878/7879。</div>
<script>
let curIp="__IP__", gen=__GEN__;
function banner(msg){const b=document.getElementById('banner');b.textContent=msg;b.style.display='block';}
function chip(label,state){return '<span class="chip '+(state===true?'ok':state===false?'bad':'warn')+'">'+label+'</span>';}
function reloadQR(){document.querySelectorAll('img.qr').forEach(im=>{im.src='/qr.png?target='+im.dataset.t+'&v='+gen+'_'+Date.now();});}
function renderNet(n){
  const c=(n&&n.checks)||{};
  const parts=[];
  parts.push(chip('子网 '+(c.subnet||'—'), c.lan_ok===true?true:null));
  // 未检到命名放行规则 ≠ 端口被封(python 可能已被放行)；故非 true 一律黄色提示，不报红
  parts.push(chip('放行7878 '+(c.fw_7878===true?'✓':c.fw_7878===false?'未检到规则':'未知'), c.fw_7878===true?true:null));
  parts.push(chip('放行7879 '+(c.fw_7879===true?'✓':c.fw_7879===false?'未检到规则':'未知'), c.fw_7879===true?true:null));
  parts.push(chip('https证书 '+(c.cert_ip_ok===true?'匹配':c.cert_ip_ok===false?'需重启更新':'未知'), c.cert_ip_ok));
  if(c.cert_chain_ok===false) parts.push(chip('证书链 不一致·重启中继重签', false));
  else if(c.cert_chain_ok===true) parts.push(chip('证书链 一致', true));
  parts.push('<span class="chip" id=recheck onclick="manualRecheck()">↻ 复检</span>');
  document.getElementById('net').innerHTML=parts.join('');
}
async function manualRecheck(){
  try{const n=await (await fetch('/netcheck',{method:'POST'})).json(); renderNet(n);}catch(e){}
}
async function tick(){
  try{
    const i=await (await fetch('/info')).json();
    if(i.ip && (i.ip!==curIp || i.ip_gen!==gen)){
      const changed=(i.ip!==curIp);
      curIp=i.ip; gen=i.ip_gen||gen;
      document.getElementById('ip').textContent='本机 IP：'+i.ip;
      document.getElementById('uhttp').textContent=i.url;
      document.getElementById('uhttps').textContent=i.https_url;
      reloadQR();
      if(changed) banner('🔄 检测到网络/IP 变化，二维码已自动更新 → 手机请重新扫码');
    }
    const n=await (await fetch('/netcheck')).json(); renderNet(n);
  }catch(e){}
  setTimeout(tick, 5000);
}
tick();
</script>
</body></html>
"""


_CERT_PAGE = r"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>免证书告警 · 安装本机证书</title>
<style>
body{margin:0;min-height:100vh;background:#080b10;color:#e5e9f5;padding:20px 18px 60px;
 font:16px/1.7 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;max-width:560px;margin:0 auto}
h1{font-size:21px;margin:.2em 0 .1em}.sub{color:#8b96b0;font-size:14px;margin-bottom:16px}
.seg{display:flex;gap:8px;margin-bottom:16px}
.seg button{flex:1;padding:10px;border-radius:10px;border:1px solid rgba(255,255,255,.14);
 background:rgba(255,255,255,.05);color:#c9d3e6;font-size:15px;font-weight:700}
.seg button.on{background:linear-gradient(135deg,#4f7aff,#a855f7);color:#fff;border-color:transparent}
.card{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.12);border-radius:14px;
 padding:16px 16px 6px;margin-bottom:14px;display:none}.card.on{display:block}
ol{margin:0;padding-left:22px}li{margin-bottom:10px}
b{color:#fde68a}.k{color:#c4b5fd;font-weight:700}
.dl{display:inline-block;margin:6px 0 10px;padding:11px 18px;border-radius:999px;font-weight:800;
 color:#fff;background:linear-gradient(135deg,#34d399,#10b981);text-decoration:none}
.go{display:inline-block;margin-top:6px;padding:10px 16px;border-radius:999px;font-weight:800;
 color:#fff;background:linear-gradient(135deg,#4f7aff,#a855f7);text-decoration:none}
.note{font-size:12.5px;color:#8b96b0;margin-top:8px;line-height:1.6}
.warn{font-size:12.5px;color:#fcd34d;background:rgba(251,191,36,.1);border:1px solid rgba(251,191,36,.35);
 border-radius:10px;padding:9px 12px;margin-top:8px}
#trust{display:flex;align-items:center;gap:10px;padding:12px 14px;border-radius:12px;margin-bottom:12px;font-size:14px;line-height:1.5}
#trust.checking{background:rgba(148,163,184,.1);border:1px solid rgba(148,163,184,.3);color:#c9d3e6}
#trust.ok{background:rgba(52,211,153,.12);border:1px solid rgba(52,211,153,.4);color:#a7f3d0}
#trust.no{background:rgba(251,191,36,.1);border:1px solid rgba(251,191,36,.35);color:#fcd34d}
#trust button{margin-left:auto;flex:none;padding:6px 12px;border-radius:999px;border:1px solid rgba(255,255,255,.22);
 background:transparent;color:inherit;font-size:12.5px;cursor:pointer}
#goBig{font-size:16px;padding:13px 20px;margin:2px 0 14px;box-shadow:0 8px 22px rgba(79,122,255,.32)}
#reinstall{color:#9bb4ff;font-size:13px;margin:0 0 10px;cursor:pointer;text-decoration:underline;
 background:none;border:0;padding:0;display:none}
</style></head><body>
<h1>🔒 一劳永逸免证书告警</h1>
<div class=sub>装一次本机证书，之后此手机打开对讲/同传页不再提示“连接不是私密”。仅本机局域网使用，私钥只在你电脑上。</div>
<div id=trust class=checking>⏳ 正在检测这台手机是否已可免告警打开对讲页…</div>
<a class=go id=goBig href="#" style="display:none">进对讲页开始同传 →</a>
<button id=reinstall onclick="showInstall()">已装过但想重装 / 换了手机？展开安装步骤</button>
<div id=installWrap>
<div class=seg>
 <button id=biOS class=on onclick="pick('ios')">iPhone / iPad</button>
 <button id=bAnd onclick="pick('and')">安卓 Android</button>
</div>

<div class=card id=cIOS>
 <ol>
  <li>点这里下载描述文件：<a class=dl href="/cert.mobileconfig">下载 iOS 描述文件</a></li>
  <li>回到手机<b>设置</b>，顶部会出现 <span class=k>已下载描述文件</span> → 点它 → <span class=k>安装</span>（右上角，输锁屏密码）。</li>
  <li><b>关键一步</b>：<span class=k>设置 → 通用 → 关于本机 → 证书信任设置</span> → 打开 <b>AvatarHub Local CA</b> 的开关。</li>
  <li>完成！回到对讲页：<a class=go id=goIOS href="#">去对讲页开始同传 →</a></li>
 </ol>
 <div class=warn>⚠️ 用 <b>Safari</b> 打开本页；微信内点右上「···→ 在浏览器打开」。第 3 步不打开信任开关会不生效。</div>
</div>

<div class=card id=cAnd>
 <ol>
  <li>点这里下载证书：<a class=dl href="/cert.pem">下载证书 (avatarhub-ca.crt)</a></li>
  <li>打开 <span class=k>设置 → 安全 → 加密与凭据 → 安装证书 → CA 证书</span>（部分机型：设置里搜“CA 证书”）。</li>
  <li>选择刚下载的 <b>avatarhub-ca.crt</b> → 确认安装（提示“可能被监控”属自签证书正常现象）。</li>
  <li>完成！回到对讲页：<a class=go id=goAnd href="#">去对讲页开始同传 →</a></li>
 </ol>
 <div class=warn>⚠️ 用 <b>Chrome</b> 打开本页；微信内点右上「···→ 在浏览器打开」。部分品牌路径为“更多安全设置 → 从存储设备安装”。</div>
</div>

<div class=note>装完仍提示告警？多为该机型把证书装成了“仅用于 VPN/应用”而非浏览器信任；可退回用扫码页的「A · 直接继续」方式，一样能用。</div>
</div>
<script>
function pick(k){
 var ios=k==='ios';
 document.getElementById('cIOS').classList.toggle('on',ios);
 document.getElementById('cAnd').classList.toggle('on',!ios);
 document.getElementById('biOS').classList.toggle('on',ios);
 document.getElementById('bAnd').classList.toggle('on',!ios);
}
var isIOS=/iPhone|iPad|iPod/i.test(navigator.userAgent||'');
pick(isIOS?'ios':'and');
var httpsBase='https://'+location.hostname+':__HTTPS_PORT__';
var httpsUrl=httpsBase+'/';
['goIOS','goAnd','goBig'].forEach(function(id){var a=document.getElementById(id);if(a)a.href=httpsUrl;});

function showInstall(){
 document.getElementById('installWrap').style.display='block';
 document.getElementById('reinstall').style.display='none';
}
function setTrust(state){
 var t=document.getElementById('trust'), wrap=document.getElementById('installWrap'),
     big=document.getElementById('goBig'), re=document.getElementById('reinstall');
 if(state==='ok'){
  t.className='ok';
  t.innerHTML='✅ 这台手机已可<b>免告警</b>直接打开对讲页！<button onclick="probeTrust()">重新检测</button>';
  big.style.display='inline-block'; wrap.style.display='none'; re.style.display='inline-block';
 }else if(state==='no'){
  t.className='no';
  t.innerHTML='还没装证书（或本机浏览器暂未信任）——按下面装一次即可。<button onclick="probeTrust()">重新检测</button>';
  big.style.display='none'; wrap.style.display='block'; re.style.display='none';
 }else{
  t.className='checking';
  t.innerHTML='⏳ 正在检测这台手机是否已可免告警打开对讲页…';
 }
}
// http 页 fetch https 子资源（HTTP→HTTPS 不触发混合内容拦截）；no-cors 下 TLS 握手成功即
// resolve(opaque)，证书未被信任/端口不可达则 reject → 据此判断“这台手机能否免告警进对讲页”。
function probeTrust(){
 setTrust('checking');
 var done=false;
 var ctl=('AbortController' in window)?new AbortController():null;
 var timer=setTimeout(function(){ if(done)return; done=true; if(ctl){try{ctl.abort();}catch(_){}} setTrust('no'); },4500);
 fetch(httpsBase+'/health',{mode:'no-cors',cache:'no-store',signal:ctl?ctl.signal:undefined})
  .then(function(){ if(done)return; done=true; clearTimeout(timer); setTrust('ok'); })
  .catch(function(){ if(done)return; done=true; clearTimeout(timer); setTrust('no'); });
}
probeTrust();
</script>
</body></html>
"""


_PAGE = r"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>监听 · 通译 LingoX</title>
<style>
:root{--acc:#4f7aff;--acc2:#a855f7;--bg:#080b10;--surface:rgba(255,255,255,.05);
 --bd:rgba(255,255,255,.12);--txt:#e5e9f5;--mut:#8b96b0;--ok:#34d399;--warn:#fbbf24;
 --me:#4f7aff;--ot:#34d399;--font:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;font:16px/1.5 var(--font);color:var(--txt);background:
 radial-gradient(120% 50% at 50% 0%,rgba(79,122,255,.16),transparent 55%),var(--bg);
 min-height:100vh;display:flex;flex-direction:column}
header{padding:12px 16px;border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:10px;
 background:var(--surface);position:sticky;top:0;backdrop-filter:blur(8px)}
.logo{width:30px;height:30px;border-radius:9px;background:linear-gradient(135deg,#4f7aff,#a855f7);
 display:grid;place-items:center;font-weight:800;color:#fff}
.t{font-weight:800;font-size:15px}.sub{font-size:11px;color:var(--mut)}
.spacer{flex:1}
#dot{width:10px;height:10px;border-radius:50%;background:#6e7681}
#dot.on{background:var(--ok);box-shadow:0 0 0 0 rgba(52,211,153,.6);animation:p 1.8s infinite}
@keyframes p{0%{box-shadow:0 0 0 0 rgba(52,211,153,.5)}70%{box-shadow:0 0 0 8px rgba(52,211,153,0)}100%{box-shadow:0 0 0 0 rgba(52,211,153,0)}}
.links{display:flex;gap:6px;overflow-x:auto;padding:7px 10px;border-bottom:1px solid var(--bd);background:rgba(255,255,255,.025)}
.chip{white-space:nowrap;font-size:11px;color:var(--mut);border:1px solid var(--bd);border-radius:999px;padding:4px 8px;background:rgba(255,255,255,.04)}
.chip.on{color:var(--ok);border-color:rgba(52,211,153,.45);background:rgba(52,211,153,.08)}
.chip.warn{color:var(--warn);border-color:rgba(251,191,36,.45);background:rgba(251,191,36,.08)}
.chip.bad{color:#f87171;border-color:rgba(248,113,113,.45);background:rgba(248,113,113,.08)}
.nav{display:flex;gap:6px;overflow-x:auto;padding:7px 10px;border-bottom:1px solid var(--bd);background:rgba(255,255,255,.018)}
.nav a,.nav button{white-space:nowrap;text-decoration:none;font-size:12px;color:var(--txt2);border:1px solid var(--bd);
 border-radius:999px;padding:6px 10px;background:rgba(255,255,255,.04)}
.nav a:hover,.nav button:hover{border-color:var(--acc);color:var(--txt)}
body.simple .expert{display:none!important}
.ctl{padding:12px 16px;display:flex;gap:10px;flex-wrap:wrap;align-items:center;border-bottom:1px solid var(--bd)}
select,button{font-family:var(--font);font-size:14px;border-radius:10px;border:1px solid var(--bd);
 background:var(--surface);color:var(--txt);padding:9px 12px}
button{cursor:pointer}
#go{flex:1;min-width:140px;font-weight:800;font-size:16px;border:none;color:#fff;padding:13px;
 background:linear-gradient(135deg,#4f7aff,#a855f7);box-shadow:0 8px 22px rgba(79,122,255,.35)}
#go.on{background:linear-gradient(135deg,#f87171,#da3633)}
#boot{flex:1;min-width:140px;font-weight:800;border-color:rgba(52,211,153,.5);background:rgba(52,211,153,.10)}
.row2{display:flex;gap:10px;align-items:center;width:100%}
.lat{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--mut);flex:1}
input[type=range]{flex:1}
.meter{height:4px;background:var(--surface);border-radius:3px;overflow:hidden;width:100%}
#followBtn{position:fixed;right:12px;bottom:14px;z-index:60;border:1px solid var(--bd);
 background:rgba(30,38,60,.92);color:var(--txt);font-size:12px;padding:8px 12px;border-radius:20px;
 box-shadow:0 4px 14px rgba(0,0,0,.35);backdrop-filter:blur(6px)}
#followBtn.paused{background:linear-gradient(135deg,#4f7aff,#a855f7);border-color:transparent;
 color:#fff;font-weight:700;animation:fpulse 1.6s ease-in-out infinite}
@keyframes fpulse{0%,100%{transform:scale(1)}50%{transform:scale(1.06)}}
.meter>i{display:block;height:100%;width:0;background:linear-gradient(90deg,#34d399,#fbbf24,#f87171);transition:width .1s}
main{flex:1;overflow-y:auto;padding:12px 14px 28px;display:flex;flex-direction:column;gap:8px}
.bub{padding:10px 14px;border-radius:16px;max-width:90%;border:1px solid var(--bd);
 background:linear-gradient(180deg,rgba(255,255,255,.05),rgba(255,255,255,.02));
 box-shadow:0 4px 14px rgba(0,0,0,.18);animation:bubIn .22s cubic-bezier(.2,.7,.3,1)}
.bub.me{align-self:flex-end;border-color:rgba(79,122,255,.5);
 background:linear-gradient(180deg,rgba(79,122,255,.16),rgba(79,122,255,.05))}
.bub.ot{align-self:flex-start;border-color:rgba(52,211,153,.5);
 background:linear-gradient(180deg,rgba(52,211,153,.14),rgba(52,211,153,.04))}
.who{font-size:11px;color:var(--mut);margin-bottom:3px;display:flex;align-items:center;gap:5px;font-weight:700;letter-spacing:.02em}
.who::before{content:'';width:7px;height:7px;border-radius:50%;background:currentColor;flex:none}
.bub.me .who{color:#9bb4ff}.bub.ot .who{color:#5ee6b0}
.zh{font-size:19px;font-weight:650;line-height:1.4}
.en{font-size:13.5px;color:var(--mut);margin-top:3px;line-height:1.35}
.bub.pend{opacity:.72}.bub.pend .zh{font-style:italic}
.bub.pend .zh::after{content:'▍';animation:blink 1s steps(1) infinite;color:var(--acc);margin-left:1px}
@keyframes bubIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@keyframes blink{50%{opacity:0}}
#interpBtn{flex:1;min-width:140px;font-weight:800;font-size:15px;border:none;color:#06281d;padding:12px;
 background:linear-gradient(135deg,#34d399,#10b981);box-shadow:0 8px 22px rgba(16,185,129,.32)}
#interpBtn.on{background:linear-gradient(135deg,#fbbf24,#f59e0b);color:#3a2a00}
#interpBtn:disabled{opacity:.6;cursor:wait}
/* P3-2 手机一键通话控制条 */
#callBtn{flex:1;min-width:140px;font-weight:800;font-size:15px;border:none;color:#fff;padding:12px;
 background:linear-gradient(135deg,#4f7aff,#a855f7);box-shadow:0 8px 22px rgba(79,122,255,.30)}
#callBtn.on{background:linear-gradient(135deg,#f87171,#da3633)}
#callBtn:disabled{opacity:.6;cursor:wait}
#panicBtn{flex:1;border-color:rgba(248,113,113,.55);color:#fca5a5;font-weight:700}
#panicBtn.on{background:linear-gradient(135deg,#f87171,#da3633);color:#fff;border-color:transparent}
#rbBtn{flex:1;font-weight:700}
#rbBtn.on{border-color:rgba(52,211,153,.6);color:#5ee6b0;background:rgba(52,211,153,.12)}
/* P8 场景方案卡(与 PC 同一后端):三预设一键切 + 双工/冲突状态 */
#sceneRow .langLb{font-size:12px;color:var(--mut);flex:0 0 auto}
#sceneRow .sc{flex:1;min-width:86px;padding:9px 4px;border-radius:11px;border:1px solid var(--bd);
 background:var(--surface2);color:var(--mut);font-size:13px;font-weight:600;text-align:center;cursor:pointer}
#sceneRow .sc.on{background:linear-gradient(135deg,#4f7aff,#a855f7);color:#fff;border-color:transparent}
#sceneStat{margin-top:6px;font-size:12px;line-height:1.65;color:var(--mut);padding:8px 10px;
 border:1px solid var(--bd);border-radius:10px;background:var(--surface2)}
#sceneStat .dup{font-weight:700}
#sceneStat .dup.full{color:#5ee6b0}
#sceneStat .dup.half{color:#fbbf24}
#sceneStat .cfl{margin-top:4px;color:#fca5a5}
/* P5-3 语向切换行 + P5-1 告警横幅 */
#langRow .langLb{font-size:12px;color:var(--mut);flex:0 0 auto}
#langRow select{flex:1;min-width:0;background:#131a2b;color:var(--txt);border:1px solid var(--bd);
 border-radius:10px;padding:8px 6px;font-size:13px}
#alertBar{display:none;width:100%;margin:0;padding:10px 14px;font-size:13px;line-height:1.5;
 border-bottom:1px solid rgba(248,113,113,.4);background:rgba(218,54,51,.16);color:#fca5a5;cursor:pointer}
#alertBar.info{border-color:rgba(79,122,255,.4);background:rgba(79,122,255,.14);color:#a5c0ff}
#alertBar b{color:inherit}
/* P6-3 结束通话弹出的本场质量卡 */
#sessCard{display:none;width:100%;padding:12px 14px;border:1px solid var(--bd);border-radius:12px;
 background:rgba(255,255,255,.04);font-size:13px;line-height:1.6;cursor:pointer}
#sessCard.open{display:block}
#sessCard .sc-t{font-weight:700;margin-bottom:4px}
#sessCard .sc-flag{color:#fca5a5;font-weight:700}
#sessCard .sc-ok{color:#5ee6b0;font-weight:700}
#sessCard .kv{color:var(--mut)} #sessCard .kv b{color:var(--txt);font-weight:600}
/* P4-3 手机端调参面板 */
#tuneBtn{flex:1;font-weight:700}
#tuneBtn.on{border-color:rgba(168,85,247,.6);color:#c4b5fd;background:rgba(168,85,247,.12)}
#tunePanel{display:none;width:100%;padding:10px;border:1px solid var(--bd);border-radius:12px;
 background:rgba(255,255,255,.03)}
#tunePanel.open{display:block}
.tnRow{display:flex;align-items:center;gap:8px;padding:5px 0}
.tnRow .tl{flex:0 0 108px;font-size:12px;color:var(--txt)}
.tnRow input[type=range]{flex:1;accent-color:#a855f7;min-width:0}
.tnRow .tv{flex:0 0 44px;text-align:right;font-size:12px;color:#c4b5fd;font-variant-numeric:tabular-nums}
.chip.live{border-color:rgba(52,211,153,.5);color:#5ee6b0;background:rgba(52,211,153,.12)}
#hint{color:var(--mut);font-size:14px;text-align:center;padding:30px 18px;line-height:1.7;margin:auto 0}
#hint .ic{font-size:42px;display:block;margin-bottom:10px;opacity:.9}
#hint b{color:var(--txt)}
#hint .hbtn{display:inline-block;margin-top:12px;padding:9px 16px;border-radius:999px;font-weight:800;
 color:#06281d;background:linear-gradient(135deg,#34d399,#10b981);cursor:pointer}
.warn{color:var(--warn);font-size:13px;text-align:center;padding:6px}
#camPrev{width:72px;height:54px;object-fit:cover;border-radius:8px;background:#111;display:none;border:1px solid var(--bd)}
#camBtn.on{border-color:rgba(168,85,247,.6);background:rgba(168,85,247,.15)}
#camHint{font-size:11px;color:var(--mut);width:100%;text-align:center;min-height:14px}
#vad.on{border-color:rgba(52,211,153,.6);background:rgba(52,211,153,.15)}
#vadHint{font-size:11px;color:var(--mut);width:100%;text-align:center;min-height:14px}
#guide{font-size:12px;color:var(--mut);width:100%;line-height:1.45;text-align:center;min-height:18px}
#guide.on{color:var(--ok)}#guide.warn{color:var(--warn)}#guide.bad{color:#f87171}
/* ── 扫码后「下一步」引导条（P0）── */
#coach{margin:10px 14px 0;border:1px solid var(--bd);border-radius:14px;overflow:hidden;
 background:linear-gradient(180deg,rgba(79,122,255,.15),rgba(168,85,247,.06))}
#coach.ok{background:linear-gradient(180deg,rgba(52,211,153,.15),rgba(52,211,153,.04));border-color:rgba(52,211,153,.42)}
#coach.bad{background:linear-gradient(180deg,rgba(248,113,113,.17),rgba(248,113,113,.05));border-color:rgba(248,113,113,.46)}
#coach .ch{display:flex;align-items:center;gap:10px;padding:12px 14px;cursor:pointer}
#coach .cn{width:26px;height:26px;flex:none;border-radius:50%;display:grid;place-items:center;
 font-weight:800;font-size:14px;background:var(--acc);color:#fff}
#coach.ok .cn{background:var(--ok);color:#06281d}#coach.bad .cn{background:#f87171;color:#3a0b0b}
#coach .cttl{font-weight:800;font-size:14.5px;line-height:1.35}
#coach .csub{font-size:12px;color:var(--mut);margin-top:2px}
#coach .cx{margin-left:auto;color:var(--mut);font-size:12px;flex:none}
#coach .cbody{display:none;padding:0 14px 13px 50px;font-size:13px;line-height:1.65;color:var(--mut)}
#coach.open .cbody{display:block}#coach .cbody b{color:var(--txt)}
#coach .cact{display:inline-block;margin-top:9px;padding:8px 15px;border-radius:999px;font-weight:800;
 color:#fff;background:linear-gradient(135deg,#4f7aff,#a855f7);cursor:pointer;border:none}
#boot.coachlit{animation:cpulse 1.5s ease-in-out infinite}
@keyframes cpulse{0%,100%{box-shadow:0 0 0 2px rgba(52,211,153,.28)}50%{box-shadow:0 0 0 7px rgba(52,211,153,.04)}}
</style></head><body class=simple>
<header>
 <div class=logo>译</div>
 <div><div class=t>监听 · LingoX</div><div class=sub id=st>未连接</div></div>
 <div class=spacer></div><span id=dot></span>
</header>
<div class=nav>
 <a id=hubLink target=_blank>← 主控台</a>
 <a id=interpLink target=_blank>实时同传</a>
 <a id=showLink target=_blank>扫码页</a>
 <a id=helpLink target=_blank>📖 教程</a>
 <button id=expertBtn type=button>专家模式</button>
</div>
<div class=links>
 <span class=chip id=lkAudio>音频:未连</span>
 <span class=chip id=lkSubs>字幕:未连</span>
 <span class=chip id=lkInterp>同传:检测中</span>
 <span class=chip id=lkMic>麦:待机</span>
 <span class=chip id=lkCam>摄像头:待机</span>
</div>
<div id=coach class=open>
 <div class=ch id=coachHead>
  <span class=cn id=coachNum>·</span>
  <div style="flex:1;min-width:0"><div class=cttl id=coachTtl>正在检测…</div><div class=csub id=coachSub></div></div>
  <span class=cx id=coachChev>展开 ▾</span>
 </div>
 <div class=cbody id=coachBody></div>
</div>
<div id=alertBar title="点击暂时收起"></div>
<div class=ctl>
 <div class=row2>
  <button id=boot title="按推荐顺序自动完成手机端准备">🚀 一键准备</button>
  <button id=go>▶ 开始监听</button>
 </div>
 <div class=row2>
  <button id=interpBtn title="启动/停止 PC 上的同传翻译会话(字幕的来源)">▶ 开始同传</button>
 </div>
 <div class=row2>
  <button id=dubFixBtn title="拉起配音引擎：显示为『无音色·仅字幕』时多半是配音引擎(Fish/CosyVoice)没起,点一下把它拉起来" style="display:none;border-color:var(--warn)">🔧 拉起配音引擎</button>
 </div>
 <div class=row2 id=sceneRow>
  <span class=langLb>场景</span>
  <span id=scChips style="display:flex;gap:6px;flex:1;flex-wrap:wrap"></span>
 </div>
 <div id=sceneStat style="display:none"></div>
 <div class=row2>
  <button id=callBtn title="PC 一键通话模式：默认麦切CABLE→设备按名绑定→链路自检→开播。微信通话用这个">📞 一键通话</button>
 </div>
 <div class=row2>
  <button id=panicBtn title="急停：0.1 秒切断正在播的克隆音(说错话/误识别时按)。再按恢复">⛔ 急停</button>
  <button id=rbBtn title="对方译文朗读：对方外语翻成中文后用 TA 自己的音色读到 PC 耳机(免盯字幕)">🔈 朗读</button>
  <button id=tuneBtn title="实战调参：音量/回声闸/声纹门槛,拖滑杆即时生效(与 PC 同一份持久化)">🎚 调参</button>
 </div>
 <div class=row2 id=langRow>
  <span class=langLb>我说</span><select id=langSrc title="我的语言"></select>
  <span class=langLb>对方</span><select id=langDst title="对方的语言"></select>
 </div>
 <div id=sessCard title="点击收起"></div>
 <div id=tunePanel>
  <div id=tuneRows><span style="color:var(--mut);font-size:12px">加载中…</span></div>
  <div class=row2 style="margin-top:6px">
   <button id=tuneReset style="flex:1;font-size:12px;padding:8px">↩ 全部回默认</button>
  </div>
 </div>
 <div id=guide></div>
 <div class="row2 expert">
  <select id=dev title="对方声所在的输出设备" style="flex:1"></select>
  <button id=auto title="扫描并自动选有声音的设备">🔍 自动</button>
 </div>
 <div class="row2 expert">
  <div class=lat>缓冲 <input type=range id=lat min=60 max=400 step=20 value=140><span id=latv>140ms</span></div>
 </div>
 <div class="meter expert"><i id=mtr></i></div>
 <div class="row2 expert" style="margin-top:4px">
  <button id=rec title="自动判定并推荐手机麦注入方案">✨ 推荐方案</button>
  <button id=align title="跟随解释器当前的麦，自动对齐注入口">🔗 跟随解释器</button>
  <button id=cablePlan title="检测克隆音与手机麦是否共用 VB-Cable">🧭 分线</button>
  <button id=alertTest title="注入一条测试告警,10秒内手机应弹横幅(同时走PC托盘)">🔔 试告警</button>
 </div>
 <div class=row2>
  <button id=talk title="按住把手机麦克风送到 PC 当本人麦">🎤 按住说话</button>
  <button id=vad title="常开监听，有声音时自动发送">🖐 常开</button>
  <select id=micdev class=expert style="flex:1" title="把手机麦注入到这个虚拟声卡(解释器选它当本人麦)"></select>
 </div>
 <div class=meter><i id=micmtr style="background:linear-gradient(90deg,#4f7aff,#a855f7)"></i></div>
 <div id=vadHint></div>
 <div id=pair class=expert style="font-size:12px;color:#c4b5fd;width:100%;text-align:center;min-height:16px"></div>
 <div id=cableHint class=expert style="font-size:12px;color:#8b96b0;width:100%;line-height:1.45"></div>
 <div class=row2 style="margin-top:4px">
  <button id=camBtn title="把手机摄像头推到 PC(换脸源)">📷 开摄像头</button>
  <video id=camPrev autoplay playsinline muted></video>
 </div>
 <div id=camHint></div>
</div>
<div class=warn id=wn></div>
<main id=log><div id=hint><span class=ic>💬</span><span id=hintTxt>正在检测同传状态…</span></div></main>
<script>
const $=s=>document.querySelector(s);
let ctx=null, ws=null, es=null, running=false, playTime=0, srv=48000;
let target=0.14;                       // 目标缓冲(秒)
$('#lat').oninput=e=>{target=e.target.value/1000;$('#latv').textContent=e.target.value+'ms';};
function setExpert(on){
  document.body.classList.toggle('simple', !on);
  $('#expertBtn').textContent = on ? '简易模式' : '专家模式';
  try{localStorage.setItem('monitor_expert', on?'1':'0');}catch(_){}
}
setExpert((()=>{try{return localStorage.getItem('monitor_expert')==='1'}catch(_){return false}})());
$('#expertBtn').onclick=()=>setExpert(document.body.classList.contains('simple'));
function setupNav(info){
  const host=(info&&info.ip)||location.hostname;
  $('#hubLink').href='http://'+host+':9000/';
  $('#interpLink').href='http://'+host+':7900/';
  $('#showLink').href='http://'+host+':7878/show';
  var hl=$('#helpLink'); if(hl) hl.href='http://'+host+':9000/help?doc=interp';
}
function link(id,txt,cls=''){
  const el=$(id); if(!el)return;
  el.textContent=txt; el.className='chip '+cls;
}

async function loadDevs(){
  try{const d=await (await fetch('/devices')).json();
    $('#dev').innerHTML=(d.speakers||[]).map(s=>`<option value="${s.name}" ${s.default?'selected':''}>${s.default?'★ ':''}${s.name}</option>`).join('');
  }catch(e){}
}
$('#dev').onchange=async()=>{ try{await fetch('/select?dev='+encodeURIComponent($('#dev').value),{method:'POST'});}catch(e){} };
function flash(m){$('#wn').textContent=m;clearTimeout(window._w);window._w=setTimeout(()=>$('#wn').textContent='',5000);}
function guide(m, cls=''){
  const el=$('#guide'); if(!el)return;
  el.className=cls; el.innerHTML=m;
}
$('#auto').onclick=async()=>{
  $('#auto').disabled=true; const old=$('#auto').textContent; $('#auto').textContent='扫描中…';
  try{const r=await (await fetch('/autopick',{method:'POST'})).json();
    if(r.picked){await loadDevs(); $('#dev').value=r.picked; flash('已自动选: '+r.picked);}
    else flash('没检测到有声音的设备 — 请让对方此刻在说话再点一次');
  }catch(e){flash('扫描失败');}
  $('#auto').textContent=old; $('#auto').disabled=false;
};

async function oneKeySetup(){
  const b=$('#boot'); b.disabled=true; const old=b.textContent; b.textContent='准备中…';
  try{
    guide('1/6 刷新设备列表…','warn');
    await loadDevs(); await loadMicDevs();

    guide('2/6 自动选择对方声输出设备…','warn');
    try{
      const a=await (await fetch('/autopick',{method:'POST'})).json();
      if(a.picked){await loadDevs(); $('#dev').value=a.picked;}
    }catch(_){}

    guide('3/6 检测 VB-Cable 分线…','warn');
    try{const p=await (await fetch('/mic/cable_plan')).json(); renderCablePlan(p);}catch(_){}

    guide('4/6 推荐并套用手机麦注入口…','warn');
    try{
      const r=await (await fetch('/mic/recommend')).json();
      if(r.chosen){
        await fetch('/mic/select?dev='+encodeURIComponent(r.chosen),{method:'POST'});
        await loadMicDevs(); $('#micdev').value=r.chosen; showPair(r.pair);
      }
    }catch(_){}

    guide('5/7 开启监听音频和字幕…','warn');
    await ensureListen();

    guide('6/7 启动同传翻译会话(字幕来源)…','warn');
    try{ if(!interpRunning) await startInterp(); }catch(e){ flash('启动同传失败: '+e.message); }

    if(!HTTPS){
      guide('监听+同传已开启。手机麦/摄像头需要 HTTPS：<a style="color:#9bf" href="'+(httpsUrl||'')+'">点此切到对讲页</a>','warn');
      return;
    }

    guide('7/7 请求麦克风和摄像头权限…','warn');
    try{ if(!vadMode) await toggleVad(true); }catch(e){ flash('常开麦启动失败: '+e.message); }
    try{ if(!camOn) await startCam(); }catch(e){ flash('摄像头启动失败: '+e.message); }
    guide('完成：监听、字幕、同传、常开麦、摄像头已按推荐流程启动。请看顶部状态灯确认。','on');
  }catch(e){
    guide('一键准备失败：'+e.message,'bad');
  }finally{
    b.disabled=false; b.textContent=old;
  }
}
$('#boot').onclick=oneKeySetup;

function startAudio(){
  ctx=new (window.AudioContext||window.webkitAudioContext)();
  playTime=0;
  link('#lkAudio','音频:连接中','warn');
  ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws/monitor');
  ws.binaryType='arraybuffer';
  ws.onopen=()=>{$('#st').textContent='已连接 · 监听中';$('#dot').className='on';link('#lkAudio','音频:在线','on');};
  ws.onclose=()=>{$('#st').textContent='已断开';$('#dot').className='';link('#lkAudio',running?'音频:重连中':'音频:停止',running?'warn':'');if(running)setTimeout(()=>{if(running)startAudio();},800);};
  ws.onmessage=ev=>{
    if(typeof ev.data==='string'){try{srv=JSON.parse(ev.data).sr||48000;}catch(e){}return;}
    const i16=new Int16Array(ev.data); if(!i16.length)return;
    const f32=new Float32Array(i16.length); let peak=0;
    for(let i=0;i<i16.length;i++){const v=i16[i]/32768;f32[i]=v;const a=v<0?-v:v;if(a>peak)peak=a;}
    $('#mtr').style.width=Math.min(100,peak*140)+'%';
    const buf=ctx.createBuffer(1,f32.length,srv); buf.getChannelData(0).set(f32);
    const src=ctx.createBufferSource(); src.buffer=buf; src.connect(ctx.destination);
    const now=ctx.currentTime;
    if(playTime<now+0.02||playTime>now+target+0.4) playTime=now+target;  // 欠载/积压→重置
    src.start(playTime); playTime+=buf.duration;
  };
}
let subsWanted=false;
function startSubs(){
  subsWanted=true;
  try{es&&es.close();}catch(_){}
  link('#lkSubs','字幕:连接中','warn');
  es=new EventSource('/subs');
  es.onopen=()=>link('#lkSubs','字幕:在线','on');
  es.onmessage=e=>{link('#lkSubs','字幕:在线','on');try{addRow(JSON.parse(e.data));}catch(_){}}
  es.onerror=()=>{link('#lkSubs',subsWanted?'字幕:重连中':'字幕:停止',subsWanted?'warn':'');};
}
// ── 同传(PC通译)会话：状态轮询 + 一键启停。字幕的真正来源 ──
let interpRunning=false, interpReachable=false, interpBusy=false, dubBusy=false;
function updateHint(){
  const log=$('#log'); const hasRows=!!log.querySelector('.bub');
  let h=$('#hint');
  if(hasRows){ if(h) h.style.display='none'; return; }
  if(!h){ h=document.createElement('div'); h.id='hint';
    h.innerHTML='<span class=ic>💬</span><span id=hintTxt></span>'; log.appendChild(h); }
  h.style.display=''; const ic=h.querySelector('.ic'), tx=$('#hintTxt'); if(!tx)return;
  if(!interpReachable){ ic.textContent='🔌';
    tx.innerHTML='连不上同传服务 — 请确认电脑端「通译 LingoX」已打开(端口 7900)。'; return; }
  if(!interpRunning){ ic.textContent='⏸';
    tx.innerHTML='<b>同传还没开始</b><br>字幕来自同传翻译，点下面按钮即可开始（用手机麦说话出字幕）'
      +'<br><span class=hbtn id=hintStart>▶ 开始同传</span>';
    const hb=$('#hintStart'); if(hb) hb.onclick=startInterp; return; }
  ic.textContent='🎙'; tx.innerHTML='<b>同传运行中</b> · 对着手机麦说话，这里会实时显示中英字幕。'
    +(running?'':'<br><span style="opacity:.8">想同时听到对方声，点上面「▶ 开始监听」</span>');
}
async function pollInterp(){
  try{
    const s=await (await fetch('/interp/status')).json();
    interpReachable=!!s.reachable; interpRunning=!!s.running;
    if(interpReachable) loadLangs();
    if(!interpReachable) link('#lkInterp','同传:离线','');
    else if(interpRunning && s.voice_ok===false) link('#lkInterp','同传:无音色·仅字幕','warn');
    else if(interpRunning) link('#lkInterp','同传:运行中','live');
    else link('#lkInterp','同传:未启动','warn');
    if(!interpBusy){ const b=$('#interpBtn');
      b.textContent=interpRunning?'■ 停止同传':'▶ 开始同传';
      b.className=interpRunning?'on':''; b.disabled=!interpReachable; }
    // 「无音色·仅字幕」多半是配音引擎没起→露出一键拉起按钮(引擎已跑则拉起幂等无害)
    const df=$('#dubFixBtn');
    if(df) df.style.display=(interpRunning && s.voice_ok===false && !dubBusy)?'':'none';
    syncCallBar(s);
    syncScene(s);
  }catch(_){ interpReachable=false; interpRunning=false; link('#lkInterp','同传:离线',''); syncCallBar(null); }
  updateHint();
}
// ── P8 场景方案卡：三预设(手机随身/电脑直连/耳机专业)一键切换,双工/冲突态随 PC 同步 ──
let scActive='', scBusy=false, scProfiles=null;
async function loadScene(){
  try{
    const r=await (await fetch('/interp/audio_profile',{cache:'no-store'})).json();
    if(!r||!r.ok) return;
    scActive=r.active; scProfiles=r.profiles;
    $('#scChips').innerHTML=Object.entries(r.profiles).map(([k,p])=>
      '<span class="sc'+(k===scActive?' on':'')+'" data-k="'+k+'" title="'+(p.desc||'')+'">'+(p.icon||'')+' '+(p.label||k)+'</span>').join('');
    document.querySelectorAll('#scChips .sc').forEach(el=>el.onclick=()=>switchScene(el.dataset.k));
    renderSceneStat(r.half_duplex_now, r.coupling, null);
  }catch(_){}
}
function renderSceneStat(hd, cp, conflicts){
  const el=$('#sceneStat'); if(!el) return;
  const p=(scProfiles&&scProfiles[scActive])||{};
  let html='';
  if(p.desc) html+='<div style="margin-bottom:3px">'+p.desc+'</div>';
  if(typeof hd==='boolean'){
    html+='<span class="dup '+(hd?'half':'full')+'">'+(hd?'⇅ 轮流说话(半双工)':'⇄ 全双工')+'</span>';
    if(cp&&cp.detail) html+=' · '+cp.detail;
  }
  if(conflicts&&conflicts.length)
    html+='<div class=cfl>'+conflicts.map(i=>(i.level==='red'?'🔴 ':'🟡 ')+i.msg+'（'+i.fix+'）').join('<br>')+'</div>';
  el.innerHTML=html; el.style.display=html?'':'none';
}
async function switchScene(k){
  if(scBusy||k===scActive) return; scBusy=true;
  try{
    const r=await (await fetch('/interp/audio_profile',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({active:k})})).json();
    if(r&&r.ok){ scActive=r.active; scProfiles=r.profiles;
      loadScene();
      guide('设备方案已切换'+(r.running?'，重新点「一键通话」生效':''),'on'); }
    else guide('切换失败: '+((r&&r.detail)||'通译不可达'),'bad');
  }catch(e){ guide('切换失败: '+e.message,'bad'); }
  scBusy=false;
}
let sceneLoaded=false;
function syncScene(s){
  if(!s||!s.reachable) return;
  if(!sceneLoaded){ sceneLoaded=true; loadScene(); }
  else if(s.audio_profile&&s.audio_profile!==scActive&&!scBusy){ scActive=s.audio_profile; loadScene(); }
  renderSceneStat(s.half_duplex, s.coupling, s.conflicts);
}
// ── P3-2 通话控制条：状态跟随 PC 侧(轮询同源 /interp/status)，按钮=遥控器 ──
// syncCallBar(状态对象)=吸收 PC 真值+可达性；syncCallBar()=仅按本地乐观态重画(点击瞬间用)。
let panicOn=false, rbOn=false, rbRef=false, callBusy=false, callOn=false;
function syncCallBar(s){
  const cb=$('#callBtn'), pb=$('#panicBtn'), rb=$('#rbBtn');
  if(!cb) return;
  if(s!==undefined){
    const reach=!!(s&&s.reachable);
    if(!callBusy){ callOn=!!(s&&s.running&&!s.live_mode); cb.disabled=!reach; }
    if(reach){ panicOn=!!s.muted; rbOn=!!s.readback_on; rbRef=!!s.readback_ref; }
    pb.disabled=!reach; rb.disabled=!reach;
  }
  if(!callBusy){
    cb.textContent=callOn?'■ 结束通话':'📞 一键通话';
    cb.className=callOn?'on':'';
  }
  pb.textContent=panicOn?'▶ 恢复':'⛔ 急停'; pb.className=panicOn?'on':'';
  rb.textContent=rbOn?(rbRef?'🔈 朗读·开':'🔈 等对方开口'):'🔈 朗读';
  rb.className=rbOn?'on':'';
}
$('#callBtn').onclick=async()=>{
  const b=$('#callBtn'); callBusy=true; b.disabled=true;
  try{
    if(callOn){
      b.textContent='结束中…';
      await fetch('/interp/call/stop',{method:'POST'});
      callOn=false; guide('通话模式已结束,系统默认麦已还原。','');
    }else{
      b.textContent='📞 准备中…(自检约10秒)';
      guide('正在准备通话：切默认麦→绑定设备→链路自检→开播…','warn');
      const r=await (await fetch('/interp/call/start',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).json();
      const lines=(r.steps||[]).map(s=>(s.ok?'✅':'❌')+' '+s.name+(s.detail?('：'+s.detail):'')).join('<br>');
      if(r.ready){ callOn=true; if(!subsWanted) startSubs();
        guide('✅ 通话就绪！微信重新拨打即生效。<br><span style="opacity:.75">'+lines+'</span>','on'); }
      else guide('⚠ 自检未全部通过：<br>'+lines,'bad');
    }
  }catch(e){ guide('一键通话失败: '+e.message,'bad'); }
  callBusy=false; b.disabled=false; pollInterp();
};
$('#panicBtn').onclick=async()=>{
  panicOn=!panicOn; syncCallBar();
  try{ await fetch('/interp/panic',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({on:panicOn})}); }catch(_){}
};
$('#rbBtn').onclick=async()=>{
  rbOn=!rbOn; syncCallBar();
  try{ await fetch('/interp/readback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({on:rbOn})}); }catch(_){}
};
// ── P4-3 手机端调参：滑杆构建自 /interp/tune 元数据(与 PC 页同一登记表),300ms 防抖提交即时生效 ──
let tuneOpen=false, tuneTimers={};
function tuneFmt(v,step){ return (+v).toFixed(step>=0.1?1:2).replace(/\.?0+$/,'')||'0'; }
async function tuneLoad(){
  const box=$('#tuneRows');
  try{
    const r=await (await fetch('/interp/tune',{cache:'no-store'})).json();
    if(!r||!r.ok||!r.meta){ box.innerHTML='<span style="color:var(--mut);font-size:12px">通译不可达,无法调参</span>'; return; }
    box.innerHTML=Object.keys(r.meta).map(k=>{
      const m=r.meta[k], v=r.values[k];
      return '<div class=tnRow title="'+(m.desc||'')+'"><span class=tl>'+m.label+'</span>'
        +'<input type=range data-k="'+k+'" min="'+m.min+'" max="'+m.max+'" step="'+m.step+'" value="'+v+'">'
        +'<span class=tv id="tv_'+k+'">'+tuneFmt(v,m.step)+'</span></div>';
    }).join('');
    box.querySelectorAll('input[type=range]').forEach(inp=>{
      const k=inp.dataset.k, step=+inp.step;
      inp.oninput=()=>{
        $('#tv_'+k).textContent=tuneFmt(inp.value,step);
        clearTimeout(tuneTimers[k]);
        tuneTimers[k]=setTimeout(async()=>{
          try{ await fetch('/interp/tune',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({values:{[k]:+inp.value}})}); }catch(_){}
        },300);
      };
    });
  }catch(e){ box.innerHTML='<span style="color:var(--mut);font-size:12px">调参加载失败: '+e.message+'</span>'; }
}
$('#tuneBtn').onclick=()=>{
  tuneOpen=!tuneOpen;
  $('#tunePanel').className=tuneOpen?'open':'';
  $('#tuneBtn').className=tuneOpen?'on':'';
  if(tuneOpen) tuneLoad();
};
$('#tuneReset').onclick=async()=>{
  try{ await fetch('/interp/tune/reset',{method:'POST'}); await tuneLoad(); guide('调参已回出厂默认。',''); }
  catch(e){ guide('重置失败: '+e.message,'bad'); }
};
// ── P5-3 手机端语向切换：选择器构建自 /interp/langs(与 PC 页同一清单),改即切+PC 侧自动预热新语对 ──
let langsLoaded=false, langBusy=false;
async function loadLangs(){
  if(langsLoaded||langBusy) return; langBusy=true;
  try{
    const r=await (await fetch('/interp/langs',{cache:'no-store'})).json();
    if(!r||!r.ok||!(r.langs||[]).length) return;
    const opts=r.langs.map(l=>'<option value="'+l.code+'">'+l.name+'</option>').join('');
    $('#langSrc').innerHTML=opts; $('#langDst').innerHTML=opts;
    $('#langSrc').value=r.src; $('#langDst').value=r.dst;
    langsLoaded=true;
  }catch(_){ }finally{ langBusy=false; }
}
async function applyLangs(){
  const src=$('#langSrc').value, dst=$('#langDst').value;
  if(!src||!dst) return;
  if(src===dst){ guide('两侧语言不能相同','warn'); return; }
  try{
    const r=await (await fetch('/interp/langs',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({src,dst})})).json();
    if(r&&r.ok) guide('语向已切换：我说'+$('#langSrc').selectedOptions[0].text+' ⇄ 对方'
      +$('#langDst').selectedOptions[0].text+(r.warm_kicked?'（翻译引擎后台预热中）':''),'on');
    else guide('语向切换失败: '+((r&&r.detail)||'通译不可达'),'bad');
  }catch(e){ guide('语向切换失败: '+e.message,'bad'); }
}
$('#langSrc').onchange=applyLangs; $('#langDst').onchange=applyLangs;
// ── P5-1 告警直达手机：10s 轮询 /alerts；新告警→横幅+振动+系统通知。首轮只记不弹(不回放历史) ──
// P6-2 增强：提醒音(WebAudio,页面后台也响)+标题闪烁+回前台立即补查(后台定时器被浏览器限流)。
let alertSeen=null, alertHideUntil=0, beepCtx=null, titleTimer=null;
const titleOrig=document.title;
function alertBeep(info){
  try{
    if(!beepCtx) return;                    // 首次触摸后才建(自动播放策略)
    const t=beepCtx.currentTime;
    [0,info?null:0.28].forEach(off=>{ if(off===null) return;
      const o=beepCtx.createOscillator(), g=beepCtx.createGain();
      o.frequency.value=info?880:988; o.type='sine';
      g.gain.setValueAtTime(0.0001,t+off); g.gain.exponentialRampToValueAtTime(0.22,t+off+0.02);
      g.gain.exponentialRampToValueAtTime(0.0001,t+off+0.22);
      o.connect(g).connect(beepCtx.destination); o.start(t+off); o.stop(t+off+0.25); });
  }catch(_){}
}
function titleFlash(){
  if(titleTimer) return;
  let n=0;
  titleTimer=setInterval(()=>{ document.title=(n++%2)?titleOrig:'⚠ 通译告警'; 
    if(n>14){ clearInterval(titleTimer); titleTimer=null; document.title=titleOrig; } },700);
}
function alertShow(txt,info){
  const bar=$('#alertBar'); if(!bar) return;
  bar.innerHTML=txt; bar.className=info?'info':''; bar.style.display='block';
  try{ navigator.vibrate&&navigator.vibrate(info?120:[180,90,180]); }catch(_){}
  try{ if(Notification&&Notification.permission==='granted')
    new Notification(info?'通译事件':'通译告警',{body:bar.textContent.slice(0,120),tag:'lingox-alert'}); }catch(_){}
  alertBeep(info); if(!info) titleFlash();
}
document.addEventListener('visibilitychange',()=>{ if(!document.hidden){ alertsTick(); } });
$('#alertBar').onclick=()=>{ $('#alertBar').style.display='none'; alertHideUntil=Date.now()+120000; };
async function alertsTick(){
  try{
    const r=await (await fetch('/alerts',{cache:'no-store'})).json();
    if(!r||!r.ok) return;
    const keys=new Set();
    const fresh=[];
    Object.keys(r.active||{}).forEach(k=>{ const a=r.active[k];
      const id='A:'+k+':'+(a.since||''); keys.add(id);
      fresh.push({id,txt:'<b>⚠ '+(a.title||k)+'</b>'+(a.detail?(' — '+a.detail):''),info:false,t:a.since||0}); });
    (r.events||[]).forEach(e=>{ const id='E:'+(e.ts||'')+':'+(e.title||''); keys.add(id);
      const warn=(e.level||'')!=='info';
      fresh.push({id,txt:'<b>'+(warn?'⚠ ':'ℹ ')+(e.title||'')+'</b>'+(e.detail?(' — '+e.detail):''),info:!warn,t:e.t||0}); });
    if(alertSeen===null){ alertSeen=keys; return; }        // 首轮建账,不回放历史
    const news=fresh.filter(x=>!alertSeen.has(x.id)).sort((a,b)=>b.t-a.t);
    keys.forEach(k=>alertSeen.add(k));
    if(news.length&&Date.now()>alertHideUntil) alertShow(news[0].txt,news[0].info);
  }catch(_){}
}
$('#alertTest').onclick=async()=>{
  try{ await fetch('/alerts/test',{method:'POST'}); guide('已注入测试告警,横幅将在 10 秒内出现','on'); }
  catch(e){ guide('测试失败: '+e.message,'bad'); }
};
document.body.addEventListener('pointerdown',function once(){
  document.body.removeEventListener('pointerdown',once);
  try{ Notification&&Notification.permission==='default'&&Notification.requestPermission(); }catch(_){}
  try{ beepCtx=beepCtx||new (window.AudioContext||window.webkitAudioContext)(); }catch(_){}
});
alertsTick(); setInterval(alertsTick,10000);
// ── P6-3 会话质量卡：通话 运行→停止 的边沿触发,拉本场摘要弹卡(结束 10s 内可见) ──
let wasRunning=false, sessCardShown='';
function fmtMs(v){ return v==null?'—':(v+'ms'); }
function fmtDur(s){ if(!s)return '—'; const m=Math.floor(s/60); return m?(m+'分'+Math.round(s%60)+'秒'):(Math.round(s)+'秒'); }
async function sessCardPop(retry){
  try{
    const r=await (await fetch('/interp/last_session',{cache:'no-store'})).json();
    const s=r&&r.summary;
    if(!s||!s.ended_at){ if(retry>0) setTimeout(()=>sessCardPop(retry-1),3000); return; }
    if(s.ended_at===sessCardShown) { if(retry>0) setTimeout(()=>sessCardPop(retry-1),3000); return; }
    sessCardShown=s.ended_at;
    const flags=s.quality_flags||[];
    const c=s.counts||{}; const drops=s.drops?Object.values(s.drops).reduce((a,b)=>a+(+b||0),0):0;
    const lg=(s.langs||[]).join('→')||'';
    $('#sessCard').innerHTML=
      '<div class=sc-t>📋 本场通话小结 '+(flags.length
        ?'<span class=sc-flag>⚠ '+flags.join(' / ')+'</span>'
        :'<span class=sc-ok>✓ 质量正常</span>')+'</div>'
      +'<div class=kv>时长 <b>'+fmtDur(s.dur_s)+'</b> · 语向 <b>'+lg+'</b> · 说话段 <b>'
      +((c.a||0)+(c.b||0))+'</b>句</div>'
      +'<div class=kv>端到端延迟 <b>'+fmtMs(s.e2e_ms)+'</b> · 拦截 <b>'+drops+'</b> · 环回断点 <b>'
      +(s.discont||0)+'</b>'
      +(s.voicelock&&s.voicelock.enrolled?(' · 声纹✓'+(s.voicelock.accepts||0)+'/✗'+(s.voicelock.rejects||0)):'')
      +'</div>';
    $('#sessCard').classList.add('open');
    try{ navigator.vibrate&&navigator.vibrate(flags.length?[150,80,150]:80); }catch(_){}
    if(flags.length) alertBeep(false);
  }catch(_){ if(retry>0) setTimeout(()=>sessCardPop(retry-1),3000); }
}
$('#sessCard').onclick=()=>$('#sessCard').classList.remove('open');
setInterval(()=>{ 
  if(wasRunning&&!interpRunning&&interpReachable) sessCardPop(3);
  wasRunning=interpRunning;
},2500);
async function startInterp(){
  const b=$('#interpBtn'); interpBusy=true; b.disabled=true; const old=b.textContent; b.textContent='启动中…';
  guide('正在启动同传(手机麦无线直连 + 抓对方声)…','warn');
  try{
    const r=await (await fetch('/interp/start',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({use_phone_mic:true})})).json();
    if(r.ok){ interpRunning=true; if(!subsWanted) startSubs();   // 确保订阅字幕流，否则收不到
      guide('同传已启动 ✓ 用手机麦说话即可看到字幕','on'); }
    else { const d=r.detail||(r.result&&r.result.detail)||'未知错误';
      flash('启动同传失败: '+d); guide('启动同传失败：'+d,'bad'); }
  }catch(e){ flash('启动同传失败: '+e.message); guide('启动同传失败: '+e.message,'bad'); }
  interpBusy=false; b.disabled=false; b.textContent=old; pollInterp();
}
async function stopInterp(){
  const b=$('#interpBtn'); interpBusy=true; b.disabled=true; const old=b.textContent; b.textContent='停止中…';
  try{ await fetch('/interp/stop',{method:'POST'}); interpRunning=false; guide('同传已停止。','');}
  catch(e){ flash('停止失败: '+e.message); }
  interpBusy=false; b.disabled=false; b.textContent=old; pollInterp();
}
$('#interpBtn').onclick=()=> interpRunning?stopInterp():startInterp();
async function fixDubEngine(){
  const b=$('#dubFixBtn'); if(!b) return;
  dubBusy=true; b.disabled=true; const old=b.textContent; b.textContent='拉起中…';
  guide('正在拉起配音引擎…十几秒后再看是否出声','warn');
  try{
    const r=await (await fetch('/interp/tts_engine_restart',{method:'POST',
      headers:{'Content-Type':'application/json'},body:'{}'})).json();
    const msg=(r&&(r.reason||r.detail))||'';
    if(r&&r.ok) guide('配音引擎已拉起 ✓ '+msg,'on');
    else { flash('拉起配音引擎失败: '+(msg||'通译不可达')); guide('拉起配音引擎失败：'+(msg||'通译不可达'),'bad'); }
  }catch(e){ flash('拉起配音引擎失败: '+e.message); guide('拉起配音引擎失败: '+e.message,'bad'); }
  dubBusy=false; b.disabled=false; b.textContent=old; pollInterp();
}
$('#dubFixBtn').onclick=fixDubEngine;
async function ensureListen(){
  if(running) return;
  running=true; $('#go').textContent='■ 停止监听'; $('#go').className='on';
  try{await fetch('/select?dev='+encodeURIComponent($('#dev').value),{method:'POST'});}catch(e){}
  startAudio(); startSubs(); updateHint();
}
function stopListen(){
  running=false; subsWanted=false; $('#go').textContent='▶ 开始监听'; $('#go').className='';
  $('#dot').className=''; $('#st').textContent='已停止';
  link('#lkAudio','音频:停止'); link('#lkSubs','字幕:停止');
  try{ws&&ws.close();}catch(e){} try{es&&es.close();}catch(e){} try{ctx&&ctx.close();}catch(e){}
  updateHint();
}
$('#go').onclick=async()=>{
  if(!running){
    await ensureListen();
  }else{
    stopListen();
  }
};

// ── 字幕渲染(对齐通译 /events 语义：partial逐词 / 子句uid累积 / finalize整轮润色) ──
const rows={};        // turn -> {el,who,zhEl,enEl,parts:{uid:{zh,en}},order:[],live}
// 自动滚动：页面实际滚动的是 body(min-height 弹性布局)而非 #log，故用 scrollIntoView
// 让"真正的滚动者"跟随最新气泡。用户上滑看历史→自动暂停并亮"回到最新"按钮；点按钮恢复跟随。
let autoFollow=true;
const scroll=()=>{
  if(!autoFollow) return;
  const last=$('#log').lastElementChild;
  if(last&&last.id!=='hint') last.scrollIntoView({block:'end'});
  $('#log').scrollTop=1e9;               // 兼容 #log 自身可滚的窄屏布局
};
function setFollow(on){
  autoFollow=on;
  const b=$('#followBtn'); if(!b) return;
  b.classList.toggle('paused',!on);
  b.textContent=on?'📜 自动滚动·开':'⬇ 回到最新';
  if(on) scroll();
}
(function(){
  const b=document.createElement('button'); b.id='followBtn'; b.textContent='📜 自动滚动·开';
  b.onclick=()=>setFollow(!autoFollow);
  document.body.appendChild(b);
  // 用户向上滚离底部 >160px → 判定"在看历史"，暂停跟随(不打断阅读)
  let t=null;
  addEventListener('scroll',()=>{
    clearTimeout(t);
    t=setTimeout(()=>{
      const se=document.scrollingElement;
      const away=se.scrollHeight-se.scrollTop-se.clientHeight>160;
      if(away&&autoFollow) setFollow(false);
    },120);
  },{passive:true});
})();
function ensureRow(turn,who){
  let t=rows[turn];
  if(!t){const col=who==='me'?'me':'ot';const el=document.createElement('div');el.className='bub '+col;
    el.innerHTML='<div class=who>'+(col==='me'?'我':'对方')+'</div><div class=zh></div><div class=en></div>';
    $('#log').appendChild(el);
    const _h=$('#hint'); if(_h)_h.style.display='none';
    t={el,who,zhEl:el.querySelector('.zh'),enEl:el.querySelector('.en'),parts:{},order:[],live:null};
    rows[turn]=t;}
  return t;
}
function renderRow(t){
  if(t.live!=null){t.el.classList.add('pend');t.zhEl.textContent=t.live;t.enEl.textContent='';return;}
  t.el.classList.remove('pend');
  t.zhEl.textContent=t.order.map(u=>t.parts[u].zh).filter(Boolean).join(' ');
  t.enEl.textContent=t.order.map(u=>t.parts[u].en).filter(Boolean).join(' ');
}
function addRow(ev){
  if(ev.who==='sys'){
    if(ev.clear){$('#log').innerHTML='';for(const k in rows)delete rows[k];updateHint();}
    if(ev.warn){$('#wn').textContent=ev.warn;clearTimeout(window._w);window._w=setTimeout(()=>$('#wn').textContent='',6000);}
    return;
  }
  if(ev.finalize){const t=rows[ev.turn];if(t){t.live=null;t.parts={F:{zh:ev.zh||t.zhEl.textContent,en:ev.en||''}};t.order=['F'];renderRow(t);scroll();}return;}
  if(ev.turn==null) return;
  if(ev.retract){const t=rows[ev.turn];if(t){t.el.remove();delete rows[ev.turn];}return;}
  const t=ensureRow(ev.turn,ev.who);
  if(ev.live!=null){t.live=ev.live;renderRow(t);scroll();return;}
  if(ev.uid!=null){t.live=null;
    if(!t.parts[ev.uid]){t.parts[ev.uid]={zh:'',en:''};t.order.push(ev.uid);}
    if(ev.zh!=null)t.parts[ev.uid].zh=ev.zh;
    if(ev.en!=null)t.parts[ev.uid].en=ev.en;
    renderRow(t);scroll();}
}
// ── 对讲：手机麦 → PC(注入虚拟声卡，解释器当本人麦)。需 https(安全上下文)。──
const HTTPS=location.protocol==='https:';
let micCtx=null,micWs=null,micStream=null,micNode=null,talking=false,httpsUrl='';
async function loadMicDevs(){
  try{const d=await (await fetch('/mic/devices')).json();
    $('#micdev').innerHTML=(d.speakers||[]).map(s=>`<option value="${s.name}" ${s.name===d.current?'selected':''}>${s.name}</option>`).join('');
  }catch(e){}
}
function showPair(p){
  if(!p){$('#pair').textContent='';return;}
  if(p.exact && p.index!=null){
    let tip = p.kind==='cable' ? '（注意：此 CABLE 若同时用于克隆音输出会自激，建议独占或换第二条）' : '';
    $('#pair').innerHTML='✅ PC 解释器「我的麦」请选 → <b>#'+p.index+' '+p.name+'</b>'+(p.hostapi?' ('+p.hostapi+')':'')+tip;
  }else if(p.kind==='voicemeeter'){
    let list=(p.bbus||[]).map(b=>'#'+b.index+' '+b.name).join('　');
    $('#pair').innerHTML='⚙ Voicemeeter 需路由：在 Voicemeeter 里把该输入勾到某个 B 总线，再把解释器「我的麦」设为对应 → '+(list||'Voicemeeter Out B1/B2/B3');
  }else{
    $('#pair').innerHTML='⚠ 在 PC 解释器把「我的麦」设为含 <b>'+(p.guess_name||'对应 Output')+'</b> 的录音设备'+(p.err?'（解释器未在线，无法取索引）':'');
  }
}
$('#micdev').onchange=async()=>{
  try{const r=await (await fetch('/mic/select?dev='+encodeURIComponent($('#micdev').value),{method:'POST'})).json();
    flash('对讲注入口: '+$('#micdev').value); showPair(r.pair);
  }catch(e){}
};
$('#rec').onclick=async()=>{
  try{const r=await (await fetch('/mic/recommend')).json();
    if(r.chosen){await fetch('/mic/select?dev='+encodeURIComponent(r.chosen),{method:'POST'});
      await loadMicDevs(); $('#micdev').value=r.chosen; showPair(r.pair);
      flash('推荐注入口: '+r.chosen+(r.needs_route?'（需在 Voicemeeter 路由一次）':'（可精确配对）'));
    }else flash('未找到可用注入口');
  }catch(e){flash('推荐失败');}
};
$('#align').onclick=async()=>{
  try{const r=await (await fetch('/mic/align',{method:'POST'})).json();
    if(r.aligned){await loadMicDevs(); $('#micdev').value=r.target; $('#pair').innerHTML='✅ 已跟随解释器麦『'+r.mic+'』，注入到 <b>'+r.target+'</b>'; flash('已对齐解释器');}
    else flash(r.detail||'无法对齐');
  }catch(e){flash('对齐失败');}
};
function renderCablePlan(p){
  if(!p||!p.ok){$('#cableHint').textContent=(p&&p.detail)||'分线检测失败';return;}
  const lanes=(p.lanes||[]).map(x=>{
    const pair=x.pair&&x.pair.name?(' → '+x.pair.name):'';
    const tag=x.usable_for_phone_mic?'✅ 可给手机麦':(x.occupied_by_clone?'⚠ 克隆音占用':(x.kind==='16ch'?'🧪 实验端点':'—'));
    return '<div>'+tag+' · <b>'+x.playback+'</b>'+pair+'<br><span style="color:#8b96b0">'+x.note+'</span></div>';
  }).join('');
  $('#cableHint').innerHTML='<div style="color:#c4b5fd">分线结论：'+p.summary+'</div>'
    +(p.cloned_voice_bus?('<div>克隆音当前输出：<b>'+p.cloned_voice_bus+'</b></div>'):'')
    +lanes;
}
$('#cablePlan').onclick=async()=>{
  try{const p=await (await fetch('/mic/cable_plan')).json(); renderCablePlan(p); flash('分线检测完成');}
  catch(e){flash('分线检测失败');}
};
let vadMode=false, vadHold=0, vadThresh=0.018, micMode='ptt';
async function startMic(mode='ptt'){
  if(!HTTPS){ flash('对讲需 HTTPS：点下方链接切到安全页'); if(httpsUrl){$('#wn').innerHTML='对讲需安全页 → <a style="color:#9bf" href="'+httpsUrl+'">点此切换到 https</a>';} return; }
  if(talking){ micMode=mode; return; }
  micMode=mode; talking=true;
  $('#talk').classList.toggle('on', mode==='ptt'); $('#talk').textContent=mode==='vad'?'🎤 已常开':'● 说话中';
  $('#vad').classList.toggle('on', mode==='vad'); $('#vad').textContent=mode==='vad'?'🟢 常开中':'🖐 常开';
  $('#vadHint').textContent=mode==='vad'?'VAD：检测到说话才发送，静音不占用解释器':'';
  link('#lkMic',mode==='vad'?'麦:常开连接中':'麦:按住连接中','warn');
  try{
    micStream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false});
    micCtx=new (window.AudioContext||window.webkitAudioContext)();
    const sr=micCtx.sampleRate;
    micWs=new WebSocket('wss://'+location.host+'/ws/mic');
    micWs.onopen=()=>{link('#lkMic',mode==='vad'?'麦:常开在线':'麦:发送中','on');try{micWs.send(JSON.stringify({sr:sr}));}catch(e){}};
    micWs.onclose=()=>{const retry=vadMode; link('#lkMic',retry?'麦:重连中':'麦:待机',retry?'warn':''); if(retry){cleanupMicOnly(); setTimeout(()=>{if(vadMode)startMic('vad');},1000);} };
    const src=micCtx.createMediaStreamSource(micStream);
    micNode=micCtx.createScriptProcessor(2048,1,1);
    micNode.onaudioprocess=e=>{
      if(!micWs||micWs.readyState!==1)return;
      const f=e.inputBuffer.getChannelData(0);const i16=new Int16Array(f.length);let pk=0,rms=0;
      for(let i=0;i<f.length;i++){let v=Math.max(-1,Math.min(1,f[i]));i16[i]=v*32767;const a=v<0?-v:v;if(a>pk)pk=a;rms+=v*v;}
      rms=Math.sqrt(rms/f.length);
      $('#micmtr').style.width=Math.min(100,pk*140)+'%';
      const active = micMode==='ptt' || rms>vadThresh || pk>vadThresh*3 || vadHold>0;
      if(rms>vadThresh || pk>vadThresh*3) vadHold=12; else if(vadHold>0) vadHold--;
      if(active) micWs.send(i16.buffer);
      if(micMode==='vad'){
        $('#vadHint').textContent=(active?'正在发送':'静音待机')+' · 电平 '+rms.toFixed(3);
        link('#lkMic',active?'麦:发送中':'麦:待机监听',active?'on':'warn');
      }
    };
    src.connect(micNode); micNode.connect(micCtx.destination);
    _micDenied=false; refreshCoach();
  }catch(err){talking=false;vadMode=false;resetMicUi();
    if(/NotAllowed|Permission|Denied|Dismiss/i.test((err&&err.name||'')+(err&&err.message||''))){_micDenied=true;}
    flash('麦克风打开失败: '+err.message); refreshCoach();}
}
function resetMicUi(){
  $('#talk').classList.remove('on'); $('#talk').textContent='🎤 按住说话';
  $('#vad').classList.remove('on'); $('#vad').textContent='🖐 常开';
  $('#vadHint').textContent=''; $('#micmtr').style.width='0%';
  link('#lkMic','麦:待机');
}
function cleanupMicOnly(){
  talking=false;
  try{micNode&&micNode.disconnect();}catch(e){} try{micStream&&micStream.getTracks().forEach(t=>t.stop());}catch(e){}
  try{micWs&&micWs.close();}catch(e){} try{micCtx&&micCtx.close();}catch(e){}
  micNode=micWs=micStream=micCtx=null;
}
function stopMic(){
  vadMode=false; cleanupMicOnly(); resetMicUi(); refreshCoach();
}
async function toggleVad(force){
  const on = force==null ? !vadMode : !!force;
  if(on){ vadMode=true; await startMic('vad'); }
  else stopMic();
}
const tb=$('#talk');
tb.addEventListener('pointerdown',e=>{e.preventDefault(); if(!vadMode)startMic('ptt');});
tb.addEventListener('pointerup',e=>{e.preventDefault(); if(!vadMode)stopMic();});
tb.addEventListener('pointercancel',()=>{if(!vadMode)stopMic();});
tb.addEventListener('pointerleave',()=>{if(talking&&!vadMode)stopMic();});
$('#vad').onclick=()=>toggleVad();

fetch('/info').then(r=>r.json()).then(d=>{httpsUrl=d.https_url||'';
  setupNav(d);
  window._certIpOk=d.cert_ip_ok;
  if(!HTTPS){$('#talk').textContent='🎤 对讲(切https)'; $('#camBtn').textContent='📷 摄像头(切https)';}
  if(d.cam_url_local){$('#camHint').dataset.url=d.cam_url_local;}
  refreshCoach();}).catch(()=>{});

// ── 摄像头：手机→PC(WebRTC 主路径，WS-JPEG 兜底) ──
let camPC=null,camStream=null,camOn=false,camWS=null,camTimer=null;
async function startCamWS(){
  link('#lkCam','摄像头:JPEG连接中','warn');
  const v=document.createElement('video'); v.srcObject=camStream; v.muted=true; await v.play();
  const c=document.createElement('canvas'),x=c.getContext('2d');
  camWS=new WebSocket('wss://'+location.host+'/ws/cam'); camWS.binaryType='arraybuffer';
  await new Promise((res,rej)=>{camWS.onopen=res; camWS.onerror=rej; setTimeout(rej,5000);});
  link('#lkCam','摄像头:JPEG在线','on');
  camTimer=setInterval(()=>{
    if(!camWS||camWS.readyState!==1||!v.videoWidth)return;
    let vw=v.videoWidth, vh=v.videoHeight, ow=vw, oh=vh;
    const LONG=960, m=Math.max(vw,vh);            // 长边封顶 960：兜底路径也保住足够清晰度，又不撑爆带宽
    if(m>LONG){ const s=LONG/m; ow=Math.round(vw*s); oh=Math.round(vh*s); }
    c.width=ow; c.height=oh; x.drawImage(v,0,0,ow,oh);
    c.toBlob(b=>{if(b&&camWS&&camWS.readyState===1)camWS.send(b);},'image/jpeg',0.8);
  },1000/12);
  _camStoreHint('ws');
}
function _camStoreHint(mode){
  const u=$('#camHint').dataset.url||'';
  $('#camHint').textContent=(mode==='ws'?'(JPEG兜底) ':'')+'PC换脸源: '+u;
}
async function startCam(){
  if(!HTTPS){flash('摄像头需 HTTPS'); if(httpsUrl){$('#wn').innerHTML='摄像头需安全页 → <a style="color:#9bf" href="'+httpsUrl+'">点此切换</a>';} return;}
  if(camOn)return;
  try{
    link('#lkCam','摄像头:授权中','warn');
    camStream=await navigator.mediaDevices.getUserMedia({video:{facingMode:'user',width:{ideal:1280},height:{ideal:720},frameRate:{ideal:24,max:30}},audio:false});
    $('#camPrev').srcObject=camStream; $('#camPrev').style.display='block';
    camPC=new RTCPeerConnection({iceServers:[]});
    camStream.getTracks().forEach(t=>camPC.addTrack(t,camStream));
    // 防糊：手机端 WebRTC 默认在带宽/CPU 紧张时“降分辨率保帧率”，会把画面缩到 180p 级，
    // 导致 PC 端换脸检测不到人脸。改为“保分辨率(降帧率)”+给足码率+提示编码器重清晰度。
    try{
      const _vt=camStream.getVideoTracks()[0];
      if(_vt){ try{_vt.contentHint='detail';}catch(_){}
        try{await _vt.applyConstraints({width:{ideal:1280},height:{ideal:720},frameRate:{ideal:24,max:30}});}catch(_){}
      }
      const _se=camPC.getSenders().find(s=>s.track&&s.track.kind==='video');
      if(_se){
        const _p=_se.getParameters(); if(!_p.encodings||!_p.encodings.length)_p.encodings=[{}];
        _p.encodings[0].maxBitrate=3000000; _p.encodings[0].scaleResolutionDownBy=1; _p.encodings[0].maxFramerate=24;
        _p.degradationPreference='maintain-resolution';
        try{await _se.setParameters(_p);}catch(_){ try{delete _p.degradationPreference; await _se.setParameters(_p);}catch(__){}}
      }
    }catch(_){}
    camPC.onconnectionstatechange=()=>{
      if(camPC.connectionState==='connected'){flash('摄像头已推到 PC'); _camStoreHint('webrtc'); link('#lkCam','摄像头:WebRTC在线','on');}
      if(['failed','disconnected'].includes(camPC.connectionState)&&camOn){
        link('#lkCam','摄像头:降级中','warn');
        startCamWS().then(()=>flash('摄像头已自动降级 JPEG')).catch(()=>{flash('摄像头断开'); stopCam();});
      }
    };
    link('#lkCam','摄像头:WebRTC连接中','warn');
    await camPC.setLocalDescription(await camPC.createOffer());
    const r=await fetch('/webrtc/cam/offer',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({sdp:camPC.localDescription.sdp,type:camPC.localDescription.type})});
    if(!r.ok) throw new Error('WebRTC offer '+r.status);
    await camPC.setRemoteDescription(await r.json());
    camOn=true; $('#camBtn').textContent='📷 关摄像头'; $('#camBtn').classList.add('on');
    _camStoreHint('webrtc'); _camDenied=false; refreshCoach();
  }catch(e){
    if(/NotAllowed|Permission|Denied|Dismiss/i.test((e&&e.name||'')+(e&&e.message||''))){_camDenied=true; refreshCoach();}
    try{if(camPC)camPC.close();}catch(_){} camPC=null;
    try{await startCamWS(); camOn=true; $('#camBtn').textContent='📷 关摄像头'; $('#camBtn').classList.add('on'); flash('WebRTC失败，已切JPEG兜底');}
    catch(e2){flash('摄像头失败: '+e.message); stopCam();}
  }
}
function stopCam(){
  camOn=false; clearInterval(camTimer); camTimer=null;
  try{camWS&&camWS.close();}catch(e){} camWS=null;
  try{camPC&&camPC.close();}catch(e){} camPC=null;
  try{camStream&&camStream.getTracks().forEach(t=>t.stop());}catch(e){} camStream=null;
  $('#camPrev').style.display='none'; try{$('#camPrev').srcObject=null;}catch(e){}
  $('#camBtn').textContent='📷 开摄像头'; $('#camBtn').classList.remove('on');
  $('#camHint').textContent='';
  link('#lkCam','摄像头:待机');
}
$('#camBtn').onclick=()=>{camOn?stopCam():startCam();};

// ── 扫码后「下一步」引导 + 麦克风/摄像头误禁恢复（P0：解决“扫码授权不知怎么选”）──
let _micDenied=false,_camDenied=false,_coachManual=false;
let _coachReadyOnce=(()=>{try{return localStorage.getItem('coach_ready')==='1'}catch(_){return false}})();
function _ua(){const u=navigator.userAgent||'';
  if(/MicroMessenger/i.test(u))return 'wechat';
  if(/iPhone|iPad|iPod/i.test(u))return 'ios';
  return 'chrome';}
async function _perm(name){try{if(!navigator.permissions||!navigator.permissions.query)return 'unknown';
  return (await navigator.permissions.query({name})).state;}catch(_){return 'unknown';}}
function _recover(dev){const b=_ua();
  if(b==='wechat')return '微信里开不了'+dev+'权限：点右上角 <b>···</b> → <b>在浏览器打开</b>，再回来重试。';
  if(b==='ios')return '打开手机 <b>设置 → Safari → '+dev+'</b> 选“允许”，或点地址栏 <b>ᴀA → 网站设置</b> 打开'+dev+'，然后回来<b>刷新</b>。';
  return '点地址栏左侧 <b>🔒 / ⓘ 图标 → 网站设置/权限</b>，把'+dev+'改成“允许”，然后<b>刷新</b>本页。';}
function _coachChev(){const co=$('#coach'),c=$('#coachChev');if(c&&co)c.textContent=co.classList.contains('open')?'收起 ▴':'展开 ▾';}
function setCoachOpen(o){const co=$('#coach');if(!co)return;_coachManual=true;co.classList.toggle('open',!!o);_coachChev();}
async function refreshCoach(){
  const co=$('#coach');if(!co)return;
  const num=$('#coachNum'),ttl=$('#coachTtl'),sub=$('#coachSub'),body=$('#coachBody'),boot=$('#boot');
  co.classList.remove('ok','bad');if(boot)boot.classList.remove('coachlit');
  const micState=_micDenied?'denied':await _perm('microphone');
  const camDen=HTTPS&&(_camDenied?true:(await _perm('camera'))==='denied');
  let state;
  if(!HTTPS)state='insecure';
  else if(micState==='denied')state='mic_denied';
  else if(!(talking||vadMode))state='need_mic';
  else state='ready';
  if(state==='insecure'){
    num.textContent='!';ttl.textContent='当前是“只听”页，不能开麦说话';sub.textContent='做同传请切到安全页（对讲）';
    body.innerHTML='做同传 / 让手机当麦克风，需要安全连接。<br><span class=cact id=cGo>切到“对讲”安全页</span>'
      +'<div style="margin-top:8px">切过去后若提示“连接不是私密”——这是本机自签证书、<b>安全</b>：'
      +'安卓 Chrome 点 <b>高级 → 继续前往</b>；iPhone 点 <b>显示详情 → 访问此网站</b>。</div>';
    co.classList.add('bad');const g=$('#cGo');if(g)g.onclick=()=>{if(httpsUrl)location.href=httpsUrl;};
  }else if(state==='mic_denied'){
    num.textContent='!';ttl.textContent='麦克风被禁用了，先恢复它';sub.textContent='恢复后即可开口做同传';
    body.innerHTML=_recover('麦克风')+'<br><span class=cact id=cReload>我已允许，刷新</span>';
    co.classList.add('bad');const r=$('#cReload');if(r)r.onclick=()=>location.reload();
  }else if(state==='need_mic'){
    num.textContent='1';ttl.textContent='点【🚀 一键准备】开始同传';sub.textContent='会自动开监听+同传，并请求麦克风';
    body.innerHTML='点下方绿色 <b>🚀 一键准备</b>；手机弹出“允许使用麦克风”时请点 <b>允许</b>。'
      +'只做语音同传就够了，要露脸出镜再点“开摄像头”。'
      +'<div style="margin-top:9px"><span class=cact id=cBoot>▶ 现在点一键准备</span></div>';
    if(boot)boot.classList.add('coachlit');
    const cb=$('#cBoot');if(cb)cb.onclick=()=>{setCoachOpen(false);boot&&boot.click();};
  }else if(!interpReachable){
    num.textContent='!';ttl.textContent='麦克风就绪，但同传服务没连上';sub.textContent='电脑端「通译 LingoX」(7900) 未运行';
    body.innerHTML='手机这端已 OK。<b>字幕/翻译来自电脑端</b>，现在连不上：请在电脑上启动 <b>通译 LingoX</b>'
      +'（跑 <b>_launch_interp.bat</b> 或 boot_stack.bat），等主控台「服务状态」里“实时同传”变运行中，本页会自动恢复。';
    co.classList.add('bad');
  }else{
    num.textContent='✓';ttl.textContent='就绪，直接对着手机说话';
    sub.textContent=camDen?'语音同传就绪（摄像头被禁用）':'开口即出中英字幕';
    body.innerHTML='已开麦，说话即可翻译播出。'
      +(camDen?('<br>摄像头被禁用（不影响语音同传）。要出镜：'+_recover('摄像头')):'要露脸出镜可点下方 <b>📷 开摄像头</b>。');
    co.classList.add('ok');
    if(!_coachReadyOnce){_coachReadyOnce=true;try{localStorage.setItem('coach_ready','1');}catch(_){}}
  }
  const _allGood=(state==='ready'&&interpReachable);
  if(!_coachManual)co.classList.toggle('open',!(_allGood&&_coachReadyOnce));
  _coachChev();
}
if($('#coachHead'))$('#coachHead').onclick=()=>setCoachOpen(!$('#coach').classList.contains('open'));
(async()=>{try{for(const nm of['microphone','camera']){if(navigator.permissions&&navigator.permissions.query){
  try{const st=await navigator.permissions.query({name:nm});st.onchange=refreshCoach;}catch(_){}}}}catch(_){}})();

loadDevs(); loadMicDevs();
updateHint(); pollInterp(); setInterval(pollInterp, 3000);
refreshCoach(); setInterval(refreshCoach, 4000);
</script></body></html>
"""

if __name__ == "__main__":
    import uvicorn
    cert = _ensure_cert()
    http_cfg = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    http_srv = uvicorn.Server(http_cfg)
    if cert:
        cf, kf = cert
        try:
            _SERVED_LEAF_PEM = Path(cf).read_bytes()   # 快照 https 实际出示的叶子，供证书链一致性判断
        except Exception:
            _SERVED_LEAF_PEM = None
        threading.Thread(target=http_srv.run, daemon=True).start()   # http 后台跑
        https_cfg = uvicorn.Config(app, host="0.0.0.0", port=HTTPS_PORT, log_level="info",
                                   ssl_certfile=cf, ssl_keyfile=kf)
        uvicorn.Server(https_cfg).run()                              # https 主线程跑
    else:
        http_srv.run()                                               # 无证书→仅 http
