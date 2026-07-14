# -*- coding: utf-8 -*-
"""VPN 二维码一键配置服务。

流程: 上传 QR 图片 → 解码 URI → 推送到设备 → 设全局 → 验证连接

二维码存放目录: data/vpn_qr/
"""

import logging
import os
import time
import shutil
from typing import Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

from .device_registry import DEFAULT_DEVICES_YAML, data_file

logger = logging.getLogger(__name__)

QR_DIR = data_file("vpn_qr")
QR_DIR.mkdir(parents=True, exist_ok=True)


# ═══════════════════════════════════════════
#  QR 解码
# ═══════════════════════════════════════════

def decode_qr_image(image_path: str) -> Optional[str]:
    """从图片文件解码 QR 码，返回文本内容。

    多重策略：pyzbar（最可靠）→ cv2 原图 → cv2 多种预处理。
    """
    # 方式 1: pyzbar (对高密度V2Ray二维码识别率最高)
    try:
        from pyzbar import pyzbar as _pyzbar
        from PIL import Image
        img = Image.open(image_path)
        codes = _pyzbar.decode(img)
        if codes:
            data = codes[0].data.decode("utf-8")
            logger.info("[VPN-QR] pyzbar 解码成功: %s...", data[:40])
            return data.strip()
        # pyzbar 灰度重试
        gray = img.convert("L")
        codes = _pyzbar.decode(gray)
        if codes:
            data = codes[0].data.decode("utf-8")
            logger.info("[VPN-QR] pyzbar 灰度解码成功")
            return data.strip()
    except ImportError:
        logger.debug("[VPN-QR] pyzbar 未安装")
    except Exception as e:
        logger.debug("[VPN-QR] pyzbar 解码失败: %s", e)

    # 方式 2: OpenCV 多种预处理
    try:
        import cv2
        img = cv2.imread(image_path)
        if img is not None:
            detector = cv2.QRCodeDetector()
            # 原图
            data, _, _ = detector.detectAndDecode(img)
            if data:
                logger.info("[VPN-QR] cv2 解码成功: %s...", data[:40])
                return data.strip()
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            # 固定阈值二值化
            _, binary = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY)
            data, _, _ = detector.detectAndDecode(binary)
            if data:
                logger.info("[VPN-QR] cv2 二值化解码成功")
                return data.strip()
            # 自适应阈值（适合光照不均的截图）
            adaptive = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY, 51, 10)
            data, _, _ = detector.detectAndDecode(adaptive)
            if data:
                logger.info("[VPN-QR] cv2 自适应阈值解码成功")
                return data.strip()
            # 放大2倍重试（小图QR码）
            h, w = img.shape[:2]
            if max(h, w) < 600:
                resized = cv2.resize(img, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
                data, _, _ = detector.detectAndDecode(resized)
                if data:
                    logger.info("[VPN-QR] cv2 放大后解码成功")
                    return data.strip()
    except ImportError:
        pass
    except Exception as e:
        logger.debug("[VPN-QR] cv2 解码失败: %s", e)

    logger.warning("[VPN-QR] 所有解码方式均失败: %s", image_path)
    return None


def decode_qr_image_with_detail(image_path: str):
    """解码 QR 码，返回 (内容, 错误详情)。"""
    import os
    if not os.path.exists(image_path):
        return None, "文件不存在"
    size = os.path.getsize(image_path)
    if size < 100:
        return None, f"文件太小({size}字节)，不是有效的QR码图片"

    result = decode_qr_image(image_path)
    if result:
        return result, None

    # 提供具体的失败原因
    try:
        from PIL import Image
        img = Image.open(image_path)
        w, h = img.size
        if w < 50 or h < 50:
            return None, f"图片尺寸太小({w}x{h})，请上传更清晰的图片"
    except Exception:
        return None, "图片文件损坏或格式不支持，请上传PNG/JPG格式"

    return None, "图片中未找到QR码。建议: 1)确保图片包含完整二维码 2)或点击\"粘贴URI\"直接粘贴vless://链接"


def validate_vpn_uri(uri: str) -> Tuple[bool, str]:
    """验证 URI 是否为有效的 V2Ray 配置。"""
    if not uri:
        return False, "空内容"
    valid_prefixes = ("vless://", "vmess://", "trojan://", "ss://")
    if not any(uri.startswith(p) for p in valid_prefixes):
        return False, f"不支持的协议，需要: {', '.join(valid_prefixes)}"
    return True, "OK"


# ═══════════════════════════════════════════
#  设备 VPN 配置
# ═══════════════════════════════════════════

def setup_device_vpn(device_id: str, uri: str) -> dict:
    """在单台设备上配置 VPN: 导入 → 全局模式 → 验证。"""
    result = {
        "device_id": device_id,
        "ok": False,
        "step": "",
        "error": "",
        "ip": "",
        "country": "",
    }

    try:
        from src.behavior.vpn_manager import get_vpn_manager
        mgr = get_vpn_manager()

        # 1. 配置 VPN (导入 + 启动)
        result["step"] = "setup"
        status = mgr.setup(device_id, uri)
        if not status.connected:
            result["error"] = status.error or "连接失败"
            return result

        result["step"] = "connected"

        # 2. 验证 Geo-IP
        result["step"] = "geo_check"
        try:
            from src.behavior.geo_check import check_device_geo
            geo = check_device_geo(device_id, "")
            result["ip"] = geo.public_ip or ""
            result["country"] = geo.detected_country or ""
        except Exception:
            result["ip"] = "unknown"

        result["ok"] = True
        result["step"] = "done"
        logger.info("[VPN-QR] %s: OK (IP: %s, %s)",
                    device_id[:8], result["ip"], result["country"])

    except Exception as e:
        result["error"] = str(e)[:100]
        logger.error("[VPN-QR] %s: %s", device_id[:8], e)

    return result


def setup_local_devices(uri: str, device_ids: list = None) -> dict:
    """给本机连接的设备配置 VPN。"""
    if not device_ids:
        try:
            from src.device_control.device_manager import get_device_manager
            mgr = get_device_manager(DEFAULT_DEVICES_YAML)
            mgr.discover_devices()
            device_ids = [d.device_id for d in mgr.get_all_devices() if d.is_online]
        except Exception as e:
            return {"ok": False, "error": f"获取设备列表失败: {e}", "results": []}

    if not device_ids:
        return {"ok": False, "error": "没有在线设备", "results": []}

    results = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(setup_device_vpn, did, uri): did for did in device_ids}
        for future in as_completed(futures):
            results.append(future.result())

    ok_count = sum(1 for r in results if r["ok"])
    return {
        "ok": ok_count > 0,
        "total": len(results),
        "success": ok_count,
        "failed": len(results) - ok_count,
        "results": results,
    }


def _get_cluster_role() -> tuple:
    """返回 (role, coordinator_url)。"""
    try:
        import yaml
        cfg_path = _project_root / "config" / "cluster.yaml"
        if cfg_path.exists():
            with open(cfg_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            return cfg.get("role", "standalone"), cfg.get("coordinator_url", "")
    except Exception:
        pass
    return "standalone", ""


def _distribute_to_workers(uri: str, skip_host_id: str = "") -> list:
    """Coordinator 专用：把 URI 推送到所有在线 Worker。"""
    import json as _json
    import urllib.request

    results = []
    try:
        from src.host.multi_host import get_cluster_coordinator
        coord = get_cluster_coordinator()
        if not coord:
            return results
        overview = coord.get_overview()
        for host in overview.get("hosts", []):
            if not host.get("online"):
                continue
            if skip_host_id and host.get("host_id") == skip_host_id:
                continue
            host_ip = host.get("host_ip", "")
            host_port = host.get("port", 8000)
            host_name = host.get("host_name", "?")
            try:
                url = f"http://{host_ip}:{host_port}/vpn/apply-uri"
                data = _json.dumps({"uri": uri}).encode()
                req = urllib.request.Request(
                    url, data=data, method="POST",
                    headers={"Content-Type": "application/json"})
                resp = urllib.request.urlopen(req, timeout=120)
                worker_result = _json.loads(resp.read().decode())
                for r in worker_result.get("results", []):
                    r["host"] = host_name
                results.extend(worker_result.get("results", []))
                logger.info("[VPN] %s: %d/%d OK",
                            host_name,
                            worker_result.get("success", 0),
                            worker_result.get("total", 0))
            except Exception as e:
                results.append({
                    "device_id": f"({host_name})",
                    "host": host_name,
                    "ok": False,
                    "error": str(e)[:80],
                    "step": "forward",
                })
                logger.warning("[VPN] 转发到 %s 失败: %s", host_name, e)
    except Exception as e:
        logger.debug("[VPN] 分发跳过: %s", e)
    return results


def setup_all_devices(uri: str, device_ids: list = None) -> dict:
    """集群感知的 VPN 配置 — 任何节点都能触发全集群配置。

    行为:
    - Coordinator: 配本机 + 推送到所有 Worker
    - Worker: 配本机 + 请求 Coordinator 分发到其他 Worker
    - Standalone: 只配本机
    """
    import json as _json
    import urllib.request

    role, coordinator_url = _get_cluster_role()

    # 1. 配置本机设备
    local_result = setup_local_devices(uri, device_ids)
    all_results = list(local_result.get("results", []))

    # 2. 集群分发
    if role == "coordinator":
        # 主控：直接推送到所有 Worker
        all_results.extend(_distribute_to_workers(uri))

    elif role == "worker" and coordinator_url:
        # Worker：请求主控分发到其他 Worker
        try:
            url = coordinator_url.rstrip("/") + "/vpn/cluster-distribute"
            # 获取本机 host_id 让主控跳过自己
            import yaml
            cfg_path = _project_root / "config" / "cluster.yaml"
            host_id = ""
            try:
                with open(cfg_path, encoding="utf-8") as f:
                    host_id = (yaml.safe_load(f) or {}).get("host_id", "")
            except Exception:
                pass

            data = _json.dumps({"uri": uri, "skip_host_id": host_id}).encode()
            req = urllib.request.Request(
                url, data=data, method="POST",
                headers={"Content-Type": "application/json"})
            resp = urllib.request.urlopen(req, timeout=120)
            coord_result = _json.loads(resp.read().decode())
            all_results.extend(coord_result.get("results", []))
            logger.info("[VPN] 通过主控分发到其他节点: %d 结果",
                        len(coord_result.get("results", [])))
        except Exception as e:
            logger.warning("[VPN] 请求主控分发失败: %s", e)
            all_results.append({
                "device_id": "(Coordinator)",
                "ok": False,
                "error": f"主控分发失败: {e}",
                "step": "coordinator_forward",
            })

    ok_count = sum(1 for r in all_results if r.get("ok"))
    return {
        "ok": ok_count > 0,
        "total": len(all_results),
        "success": ok_count,
        "failed": len(all_results) - ok_count,
        "results": all_results,
    }


# ═══════════════════════════════════════════
#  QR 文件管理
# ═══════════════════════════════════════════

def save_qr_image(filename: str, content: bytes) -> str:
    """保存上传的 QR 图片，返回保存路径。"""
    # 安全文件名
    safe_name = f"vpn_{time.strftime('%Y%m%d_%H%M%S')}_{filename}"
    safe_name = "".join(c for c in safe_name if c.isalnum() or c in "._-")
    path = QR_DIR / safe_name
    path.write_bytes(content)
    logger.info("[VPN-QR] 保存: %s (%d KB)", safe_name, len(content) // 1024)
    return str(path)


def list_qr_images() -> list:
    """列出已上传的 QR 图片。"""
    result = []
    for f in sorted(QR_DIR.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp", ".webp"):
            result.append({
                "filename": f.name,
                "size_kb": round(f.stat().st_size / 1024, 1),
                "time": time.strftime("%Y-%m-%d %H:%M:%S",
                                      time.localtime(f.stat().st_mtime)),
                "uri": decode_qr_image(str(f)) or "",
            })
    return result[:20]  # 最近 20 张
