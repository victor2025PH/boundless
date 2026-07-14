# -*- coding: utf-8 -*-
"""Decode V2Ray QR code and extract configuration."""
import sys
import json
import base64
from pathlib import Path

def decode_qr_from_image(image_path: str) -> str:
    """Decode QR code from image file, trying multiple methods."""
    # Method 1: pyzbar
    try:
        from pyzbar.pyzbar import decode as pyzbar_decode
        from PIL import Image
        img = Image.open(image_path)
        results = pyzbar_decode(img)
        if results:
            return results[0].data.decode("utf-8")
    except Exception as e:
        print(f"  pyzbar failed: {e}")

    # Method 2: cv2 QRCodeDetector
    try:
        import cv2
        img = cv2.imread(image_path)
        detector = cv2.QRCodeDetector()
        data, _, _ = detector.detectAndDecode(img)
        if data:
            return data
    except Exception as e:
        print(f"  cv2 failed: {e}")

    # Method 3: cv2 with preprocessing
    try:
        import cv2
        import numpy as np
        img = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
        _, binary = cv2.threshold(img, 127, 255, cv2.THRESH_BINARY)
        detector = cv2.QRCodeDetector()
        data, _, _ = detector.detectAndDecode(binary)
        if data:
            return data
    except Exception as e:
        print(f"  cv2 binary failed: {e}")

    return ""


def parse_v2ray_uri(uri: str) -> dict:
    """Parse vmess://, vless://, ss://, trojan:// URIs."""
    if uri.startswith("vmess://"):
        payload = uri[8:]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        try:
            decoded = base64.b64decode(payload).decode("utf-8")
            config = json.loads(decoded)
            return {"protocol": "vmess", "config": config}
        except Exception:
            return {"protocol": "vmess", "raw": payload}

    elif uri.startswith("vless://"):
        return {"protocol": "vless", "uri": uri}

    elif uri.startswith("ss://"):
        return {"protocol": "shadowsocks", "uri": uri}

    elif uri.startswith("trojan://"):
        return {"protocol": "trojan", "uri": uri}

    return {"protocol": "unknown", "raw": uri}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python decode_qr.py <image_path>")
        sys.exit(1)

    img_path = sys.argv[1]
    print(f"Decoding QR from: {img_path}")

    uri = decode_qr_from_image(img_path)
    if not uri:
        print("ERROR: Could not decode QR code")
        sys.exit(1)

    print(f"\nRaw URI ({len(uri)} chars):")
    print(f"  {uri[:120]}...")

    parsed = parse_v2ray_uri(uri)
    print(f"\nProtocol: {parsed['protocol']}")
    if "config" in parsed:
        print(f"Config:")
        for k, v in parsed["config"].items():
            print(f"  {k}: {v}")
    print(f"\nFull URI (for V2RayNG import):")
    print(uri)
