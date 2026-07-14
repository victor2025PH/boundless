#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 OpenClaw 集群自签名 TLS 证书。

用法:
  python scripts/generate_certs.py

生成文件:
  config/certs/server.key  — 私钥
  config/certs/server.crt  — 自签名证书 (365天有效)
"""
import ipaddress
import subprocess
import sys
from pathlib import Path

CERT_DIR = Path(__file__).resolve().parent.parent / "config" / "certs"


def generate():
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    key_path = CERT_DIR / "server.key"
    crt_path = CERT_DIR / "server.crt"

    if key_path.exists() and crt_path.exists():
        print(f"[跳过] 证书已存在: {CERT_DIR}")
        print(f"  删除后重新运行以重新生成")
        return str(key_path), str(crt_path)

    # 使用 cryptography 库生成
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime

        # 生成RSA私钥
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        # 创建自签名证书
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "OpenClaw"),
            x509.NameAttribute(NameOID.COMMON_NAME, "openclaw.local"),
        ])

        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.utcnow())
            .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))
            .add_extension(
                x509.SubjectAlternativeName([
                    x509.DNSName("localhost"),
                    x509.DNSName("openclaw.local"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                    x509.IPAddress(ipaddress.IPv4Address("192.168.0.118")),
                    x509.IPAddress(ipaddress.IPv4Address("10.222.142.172")),
                ]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )

        # 保存
        with open(key_path, "wb") as f:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))

        with open(crt_path, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))

        print(f"[成功] 证书已生成:")
        print(f"  私钥: {key_path}")
        print(f"  证书: {crt_path}")
        return str(key_path), str(crt_path)

    except ImportError:
        # 回退: 使用 openssl CLI
        print("[INFO] cryptography 库未安装，尝试 openssl CLI...")
        cmd = [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(key_path), "-out", str(crt_path),
            "-days", "365", "-nodes",
            "-subj", "/C=CN/O=OpenClaw/CN=openclaw.local",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"[成功] 证书已生成 (openssl)")
            return str(key_path), str(crt_path)
        else:
            print(f"[错误] openssl 失败: {result.stderr}")
            print("[提示] 安装 cryptography: pip install cryptography")
            return None, None


if __name__ == "__main__":
    generate()
