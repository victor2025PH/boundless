# -*- coding: utf-8 -*-
"""
provenance.py — Phase 11 合规溯源（标准 C2PA 嵌入 + Ed25519 签名 + LSB 音频水印 + 软绑定）

对齐 EU AI Act Article 50（2026-08-02 生效，机器可读「AI 生成」标记）：
  - 标准 C2PA 嵌入(embed_c2pa_*)：若已装 c2pa-python，则在文件内嵌入标准 C2PA manifest（
    自签两级 Ed25519 证书链，validation_state=Valid，CAI/Verify/Leica 工具可直读）。
  - Ed25519 manifest 签名：非对称公开可验（verify_with_public_key），向后兼容 HMAC-SHA256。
  - LSB 音频水印：PCM 帧内嵌 sync+payload+CRC，抗重封装/裁剪；非 PCM16 时原样返回。
  - 软绑定：水印承载 payload_id，完整 manifest 入本地 SQLite，按 id 解析。

所有 c2pa-python 功能软降级：未装则回退 LSB+Ed25519；标准库模块均可独立单测。
"""
from __future__ import annotations

import io
import os
import json
import time
import wave
import uuid
import hmac
import struct
import hashlib
import sqlite3
from pathlib import Path
from typing import Optional

import app_config
BASE_DIR        = app_config.BASE
PROV_DB         = BASE_DIR / "provenance.db"          # 软绑定 manifest 仓库
PROV_KEY_FILE   = BASE_DIR / "provenance_key.bin"     # 本地签名密钥（HMAC，向后兼容）
PROV_LOG        = BASE_DIR / "provenance_log.jsonl"   # 审计日志
ED_SK_FILE      = BASE_DIR / "provenance_ed25519_sk.pem"   # Ed25519 私钥（仅本机签名）
ED_PK_FILE      = BASE_DIR / "provenance_ed25519_pk.pem"   # Ed25519 公钥（可公开分发供第三方验签）

# Ed25519 非对称签名：第三方仅凭公钥即可验真，无需密钥 → 真正的 C2PA 式来源可验证性
# （HMAC 是对称的，只有签发者能验，无法对外证明）。装了 cryptography 则启用，否则降级 HMAC。
try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey)
    from cryptography.hazmat.primitives import serialization as _crypto_ser
    _HAS_ED25519 = True
except Exception:
    _HAS_ED25519 = False

CLAIM_GENERATOR = "AvatarHub/1.0"
_BRAND_FILE = BASE_DIR / "data" / "brand.json"   # 白标配置（与 /api/brand 同源）
_DEFAULT_BRAND = "无界 BOUNDLESS"


def _brand_generator() -> str:
    """C2PA claim_generator 署名：随白标配置(data/brand.json)走，回退内置品牌。
    形如「无界 BOUNDLESS · 幻颜 FaceX」（无产品线则仅品牌名）。每次读取以即时跟随白标改动。"""
    name, product = _DEFAULT_BRAND, ""
    try:
        if _BRAND_FILE.exists():
            d = json.loads(_BRAND_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                name = (str(d.get("name") or "")).strip() or name
                product = (str(d.get("product") or "")).strip()
    except Exception:
        pass
    return (name + " · " + product) if product else name
# 水印帧：SYNC(16) + payload(128) + CRC16(16) = 160 bit
_SYNC_BITS  = 0xACED
_SYNC_LEN   = 16
_PAYLOAD_LEN_BYTES = 16          # 128 bit payload_id
_PAYLOAD_LEN_BITS  = _PAYLOAD_LEN_BYTES * 8
_CRC_LEN    = 16
_FRAME_BITS = _SYNC_LEN + _PAYLOAD_LEN_BITS + _CRC_LEN  # 160


# ── 签名密钥 ─────────────────────────────────────────────────────────
def _get_secret() -> bytes:
    try:
        if PROV_KEY_FILE.exists():
            return PROV_KEY_FILE.read_bytes()
        key = os.urandom(32)
        PROV_KEY_FILE.write_bytes(key)
        return key
    except Exception:
        # 退化：进程内固定（重启后验签可能失效，仅兜底）
        return b"avatarhub-fallback-secret-key-32b"


# ── Ed25519 非对称密钥（公开可验）──────────────────────────────────────
_ed_sk_cache = None   # 私钥对象（签名用，仅本机）
_ed_pk_cache = None   # 公钥对象（验签用，可由公钥文件加载）


def _get_ed_sk():
    """加载/生成 Ed25519 私钥。首次生成时同时落盘公钥（PEM）。无 cryptography 返回 None。"""
    global _ed_sk_cache
    if not _HAS_ED25519:
        return None
    if _ed_sk_cache is not None:
        return _ed_sk_cache
    try:
        if ED_SK_FILE.exists():
            _ed_sk_cache = _crypto_ser.load_pem_private_key(ED_SK_FILE.read_bytes(), password=None)
        else:
            _ed_sk_cache = Ed25519PrivateKey.generate()
            ED_SK_FILE.write_bytes(_ed_sk_cache.private_bytes(
                _crypto_ser.Encoding.PEM,
                _crypto_ser.PrivateFormat.PKCS8,
                _crypto_ser.NoEncryption()))
            ED_PK_FILE.write_bytes(_ed_sk_cache.public_key().public_bytes(
                _crypto_ser.Encoding.PEM,
                _crypto_ser.PublicFormat.SubjectPublicKeyInfo))
        return _ed_sk_cache
    except Exception:
        return None


def _get_ed_pk():
    """加载验签公钥：优先公钥文件；缺失则从私钥派生并补写。无则 None。"""
    global _ed_pk_cache
    if not _HAS_ED25519:
        return None
    if _ed_pk_cache is not None:
        return _ed_pk_cache
    try:
        if ED_PK_FILE.exists():
            _ed_pk_cache = _crypto_ser.load_pem_public_key(ED_PK_FILE.read_bytes())
            return _ed_pk_cache
        sk = _get_ed_sk()
        if sk is None:
            return None
        _ed_pk_cache = sk.public_key()
        try:
            ED_PK_FILE.write_bytes(_ed_pk_cache.public_bytes(
                _crypto_ser.Encoding.PEM,
                _crypto_ser.PublicFormat.SubjectPublicKeyInfo))
        except Exception:
            pass
        return _ed_pk_cache
    except Exception:
        return None


def public_key_pem() -> str:
    """返回 Ed25519 公钥 PEM（可公开分发，供任意第三方离线验签）。无则空串。"""
    pk = _get_ed_pk()
    if pk is None:
        return ""
    try:
        return pk.public_bytes(
            _crypto_ser.Encoding.PEM,
            _crypto_ser.PublicFormat.SubjectPublicKeyInfo).decode("ascii")
    except Exception:
        return ""


def verify_with_public_key(manifest: dict, signature_hex: str, public_key_pem_str: str) -> bool:
    """纯第三方验签：仅凭给定的 Ed25519 公钥 PEM 验证 manifest 签名（不触本机密钥）。"""
    if not _HAS_ED25519:
        return False
    try:
        pk = _crypto_ser.load_pem_public_key(public_key_pem_str.encode("ascii"))
        pk.verify(bytes.fromhex(signature_hex), _canonical(manifest))
        return True
    except Exception:
        return False


# ── 软绑定仓库（SQLite）──────────────────────────────────────────────
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(PROV_DB), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS manifests (
        payload_id TEXT PRIMARY KEY,
        manifest   TEXT NOT NULL,
        signature  TEXT NOT NULL,
        alg        TEXT NOT NULL,
        created_at REAL NOT NULL
    )""")
    return conn


def _store_manifest(payload_id: str, manifest: dict, signature: str, alg: str) -> None:
    try:
        with _db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO manifests VALUES (?,?,?,?,?)",
                (payload_id, json.dumps(manifest, ensure_ascii=False),
                 signature, alg, time.time()))
            conn.commit()
    except Exception:
        pass


def resolve_manifest(payload_id: str) -> Optional[dict]:
    """软绑定解析：按 payload_id 取回完整 manifest + 签名。"""
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT manifest, signature, alg, created_at FROM manifests WHERE payload_id=?",
                (payload_id,)).fetchone()
        if not row:
            return None
        return {"manifest": json.loads(row[0]), "signature": row[1],
                "alg": row[2], "created_at": row[3]}
    except Exception:
        return None


# ── manifest 构建 / 签名 ─────────────────────────────────────────────
def _canonical(manifest: dict) -> bytes:
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def sign_manifest(manifest: dict) -> tuple[str, str]:
    """返回 (signature_hex, alg)。优先 Ed25519（非对称、公开可验）；无 cryptography 降级 HMAC-SHA256。"""
    sk = _get_ed_sk()
    if sk is not None:
        try:
            sig = sk.sign(_canonical(manifest)).hex()
            return sig, "Ed25519"
        except Exception:
            pass
    sig = hmac.new(_get_secret(), _canonical(manifest), hashlib.sha256).hexdigest()
    return sig, "HMAC-SHA256"


def _verify_ed(manifest: dict, signature: str) -> bool:
    pk = _get_ed_pk()
    if pk is None:
        return False
    try:
        pk.verify(bytes.fromhex(signature), _canonical(manifest))
        return True
    except Exception:
        return False


def _verify_hmac(manifest: dict, signature: str) -> bool:
    try:
        expect = hmac.new(_get_secret(), _canonical(manifest), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expect, signature)
    except Exception:
        return False


def verify_signature(manifest: dict, signature: str, alg: str = "") -> bool:
    """验签。指定 alg 则按其分派；未指定(空)则自动判定(先 Ed25519 再 HMAC，兼容两代凭证)。"""
    try:
        if alg == "Ed25519":
            return _verify_ed(manifest, signature)
        if alg == "HMAC-SHA256":
            return _verify_hmac(manifest, signature)
        return _verify_ed(manifest, signature) or _verify_hmac(manifest, signature)
    except Exception:
        return False


def make_manifest(*, fmt: str = "audio/wav", ai_generated: bool = True,
                  model: str = "", profile: str = "", extra: Optional[dict] = None,
                  payload_id: Optional[str] = None) -> dict:
    """构建 C2PA 风格 manifest（含机器可读 AI 生成标记）。"""
    pid = payload_id or uuid.uuid4().hex
    now = time.time()
    brand = _brand_generator()   # 署名随白标，与 C2PA/视频/图片署名同源
    manifest = {
        "claim_generator": brand,
        "format": fmt,
        "instance_id": "xmp:iid:" + uuid.uuid4().hex,
        "created": now,
        "created_str": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        "soft_binding": {"alg": "avatarhub-lsb-v1", "payload_id": pid},
        "assertions": [
            {"label": "c2pa.actions", "data": {"actions": [
                {"action": "c2pa.created",
                 "softwareAgent": brand,
                 # IPTC DigitalSourceType：AI 训练算法生成
                 "digitalSourceType":
                     "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"
                 if ai_generated else
                     "http://cv.iptc.org/newscodes/digitalsourcetype/digitalCapture"}]}},
            {"label": "com.avatarhub.ai_generated",
             "data": {"ai_generated": bool(ai_generated), "model": model,
                      "profile": profile, **(extra or {})}},
        ],
    }
    return manifest


# ── 比特/CRC 工具 ────────────────────────────────────────────────────
def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
    return crc


def _bits_of(value: int, width: int) -> list[int]:
    return [(value >> (width - 1 - i)) & 1 for i in range(width)]


def _build_frame_bits(payload_id_hex: str) -> list[int]:
    payload = bytes.fromhex(payload_id_hex)[:_PAYLOAD_LEN_BYTES].ljust(_PAYLOAD_LEN_BYTES, b"\x00")
    crc = _crc16(payload)
    bits = _bits_of(_SYNC_BITS, _SYNC_LEN)
    for byte in payload:
        bits += _bits_of(byte, 8)
    bits += _bits_of(crc, _CRC_LEN)
    return bits


def _read_wav_pcm16(wav_bytes: bytes):
    """返回 (params, samples:list[int]) 或 None（非 16bit PCM）。"""
    with wave.open(io.BytesIO(wav_bytes)) as wf:
        if wf.getsampwidth() != 2:
            return None
        params = wf.getparams()
        raw = wf.readframes(wf.getnframes())
    import array
    samples = array.array("h")
    samples.frombytes(raw)
    return params, samples


def _write_wav_pcm16(params, samples) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setparams(params)
        wf.writeframes(samples.tobytes())
    return buf.getvalue()


# ── 音频水印 嵌入 / 提取 ─────────────────────────────────────────────
def embed_audio_watermark(wav_bytes: bytes, payload_id_hex: str) -> bytes:
    """在 16bit PCM 的 LSB 上重复嵌入帧（sync+payload+crc）。非 PCM16 时原样返回。"""
    parsed = _read_wav_pcm16(wav_bytes)
    if parsed is None:
        return wav_bytes
    params, samples = parsed
    frame = _build_frame_bits(payload_id_hex)
    flen = len(frame)
    n = len(samples)
    if n < flen:
        return wav_bytes  # 太短无法承载
    for i in range(n):
        bit = frame[i % flen]
        s = samples[i]
        samples[i] = (s & ~1) | bit if s >= 0 else -((-s & ~1) | bit)
    return _write_wav_pcm16(params, samples)


def extract_audio_watermark(wav_bytes: bytes) -> Optional[str]:
    """从 LSB 还原 payload_id（滑窗找 sync + CRC 校验 + 多副本多数表决）。"""
    parsed = _read_wav_pcm16(wav_bytes)
    if parsed is None:
        return None
    _, samples = parsed
    n = len(samples)
    if n < _FRAME_BITS:
        return None
    lsb = [(abs(samples[i]) & 1) for i in range(n)]
    sync = _bits_of(_SYNC_BITS, _SYNC_LEN)
    candidates: dict[str, int] = {}
    i = 0
    limit = n - _FRAME_BITS
    while i <= limit:
        if lsb[i:i + _SYNC_LEN] == sync:
            frame = lsb[i:i + _FRAME_BITS]
            payload_bits = frame[_SYNC_LEN:_SYNC_LEN + _PAYLOAD_LEN_BITS]
            crc_bits = frame[_SYNC_LEN + _PAYLOAD_LEN_BITS:]
            payload = bytearray()
            for b in range(_PAYLOAD_LEN_BYTES):
                byte = 0
                for k in range(8):
                    byte = (byte << 1) | payload_bits[b * 8 + k]
                payload.append(byte)
            crc_val = 0
            for bit in crc_bits:
                crc_val = (crc_val << 1) | bit
            if _crc16(bytes(payload)) == crc_val:
                hexid = bytes(payload).hex()
                candidates[hexid] = candidates.get(hexid, 0) + 1
                i += _FRAME_BITS
                continue
        i += 1
    if not candidates:
        return None
    return max(candidates.items(), key=lambda kv: kv[1])[0]


# ── 高层：附加 / 验证 内容凭证 ───────────────────────────────────────
def attach_credentials(wav_bytes: bytes, *, model: str = "", profile: str = "",
                       ai_generated: bool = True, extra: Optional[dict] = None) -> dict:
    """生成 manifest → 签名 → 入库（软绑定）→ LSB 水印嵌入 → 叠加真 C2PA 标准嵌入（若可用）。
    返回 {ok, audio_bytes, payload_id, manifest, signature, watermarked, c2pa_embedded}。"""
    payload_id = uuid.uuid4().hex
    manifest = make_manifest(model=model, profile=profile, ai_generated=ai_generated,
                             extra=extra, payload_id=payload_id)
    signature, alg = sign_manifest(manifest)
    _store_manifest(payload_id, manifest, signature, alg)
    out = embed_audio_watermark(wav_bytes, payload_id)
    watermarked = out is not wav_bytes and out != wav_bytes
    # 叠加标准 C2PA 嵌入（若 c2pa-python 可用）：CAI/Verify 工具可直读
    c2pa_embedded = False
    c2pa_out = embed_c2pa(out, "audio/x-wav",
                          model=model, profile=profile, ai_generated=ai_generated, extra=extra)
    if c2pa_out is not None:
        out = c2pa_out
        c2pa_embedded = True
    _audit("attach", payload_id=payload_id, model=model, profile=profile,
           watermarked=watermarked, c2pa_embedded=c2pa_embedded)
    return {"ok": True, "audio_bytes": out, "payload_id": payload_id,
            "manifest": manifest, "signature": signature, "alg": alg,
            "watermarked": watermarked, "c2pa_embedded": c2pa_embedded}


def verify_credentials(wav_bytes: bytes) -> dict:
    """提取水印 → 软绑定解析 manifest → 验签。
    返回 {has_watermark, payload_id, manifest, signature_valid, ai_generated, ...}。"""
    pid = extract_audio_watermark(wav_bytes)
    if not pid:
        return {"has_watermark": False, "payload_id": "", "manifest": None,
                "signature_valid": False, "ai_generated": None}
    rec = resolve_manifest(pid)
    if not rec:
        return {"has_watermark": True, "payload_id": pid, "manifest": None,
                "signature_valid": False, "ai_generated": None,
                "note": "水印存在但本地仓库无 manifest（可能由其他实例生成）"}
    valid = verify_signature(rec["manifest"], rec["signature"], rec.get("alg", "HMAC-SHA256"))
    ai = None
    for a in rec["manifest"].get("assertions", []):
        if a.get("label") == "com.avatarhub.ai_generated":
            ai = a.get("data", {}).get("ai_generated")
    return {"has_watermark": True, "payload_id": pid, "manifest": rec["manifest"],
            "signature_valid": valid, "ai_generated": ai,
            "created_str": rec["manifest"].get("created_str", "")}


def detect_ai_generated(wav_bytes: bytes) -> dict:
    """对素材做 AI 生成检测。当前：识别本系统凭证(强证据)；外部内容需挂神经检测模型。
    返回 {ai_generated, confidence, method}。"""
    v = verify_credentials(wav_bytes)
    if v.get("has_watermark") and v.get("signature_valid") and v.get("ai_generated"):
        return {"ai_generated": True, "confidence": 0.99,
                "method": "avatarhub-credentials"}
    if v.get("has_watermark"):
        return {"ai_generated": True, "confidence": 0.8,
                "method": "watermark-present"}
    return {"ai_generated": None, "confidence": 0.0,
            "method": "no-provenance（需外部神经检测模型，详见 Phase11 备注）"}


def _audit(event: str, **fields) -> None:
    try:
        entry = {"ts": time.time(), "ts_str": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "event": event, **fields}
        with open(PROV_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════
# 标准 C2PA 嵌入（需 c2pa-python；未装时所有函数返回 None 并软降级）
# ══════════════════════════════════════════════════════════════════════
try:
    from c2pa import Builder as _C2paBuilder, Signer as _C2paSigner
    from c2pa import C2paSigningAlg as _C2paAlg, Reader as _C2paReader
    _HAS_C2PA = True
except Exception:
    _HAS_C2PA = False

# 持久化证书链路径（两级 Ed25519 自签链：CA + 终端实体）
_C2PA_CA_CERT    = BASE_DIR / "c2pa_ca_cert.pem"
_C2PA_CA_KEY     = BASE_DIR / "c2pa_ca_key.pem"
_C2PA_EE_CERT    = BASE_DIR / "c2pa_ee_cert.pem"
_C2PA_EE_KEY     = BASE_DIR / "c2pa_ee_key.pem"
_C2PA_CHAIN_PEM  = BASE_DIR / "c2pa_chain.pem"   # EE + CA（供 c2pa 传入）

# C2PA claim-signing EKU OIDs
_OID_DOC_SIGN  = "1.3.6.1.5.5.7.3.36"          # RFC 9336 documentSigning
_OID_C2PA_SIGN = "1.3.6.1.4.1.62558.1.1"       # c2pa-kp-claimSigning


def _ensure_c2pa_certs() -> Optional[tuple[str, "Ed25519PrivateKey"]]:
    """确保两级 Ed25519 证书链存在（首次自动生成并落盘）。
    返回 (chain_pem_str, ee_sk) 或 None。需 cryptography 包。"""
    if not _HAS_ED25519:
        return None
    try:
        import datetime
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import serialization as _s
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        # 加载已有链
        if _C2PA_CHAIN_PEM.exists() and _C2PA_EE_KEY.exists():
            chain_pem = _C2PA_CHAIN_PEM.read_text("ascii")
            ee_sk = _s.load_pem_private_key(_C2PA_EE_KEY.read_bytes(), password=None)
            return chain_pem, ee_sk

        # 生成 CA 密钥+证书
        now = datetime.datetime.now(datetime.timezone.utc)
        ca_sk = Ed25519PrivateKey.generate()
        ca_name = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "AvatarHub Root CA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "AvatarHub"),
        ])
        ca_cert = (x509.CertificateBuilder()
                   .subject_name(ca_name).issuer_name(ca_name)
                   .public_key(ca_sk.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(days=1))
                   .not_valid_after(now + datetime.timedelta(days=3650))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                   .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_sk.public_key()), critical=False)
                   .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False,
                                                key_encipherment=False, data_encipherment=False,
                                                key_agreement=False, key_cert_sign=True, crl_sign=True,
                                                encipher_only=False, decipher_only=False), critical=True)
                   .sign(private_key=ca_sk, algorithm=None))

        # 生成终端实体密钥+证书
        ee_sk = Ed25519PrivateKey.generate()
        ee_name = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "AvatarHub Provenance Signer"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "AvatarHub"),
        ])
        ee_cert = (x509.CertificateBuilder()
                   .subject_name(ee_name).issuer_name(ca_name)
                   .public_key(ee_sk.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(days=1))
                   .not_valid_after(now + datetime.timedelta(days=3650))
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .add_extension(x509.SubjectKeyIdentifier.from_public_key(ee_sk.public_key()), critical=False)
                   .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_sk.public_key()), critical=False)
                   .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                                key_encipherment=False, data_encipherment=False,
                                                key_agreement=False, key_cert_sign=False, crl_sign=False,
                                                encipher_only=False, decipher_only=False), critical=True)
                   .add_extension(x509.ExtendedKeyUsage([
                       x509.ObjectIdentifier(_OID_DOC_SIGN),
                       x509.ObjectIdentifier(_OID_C2PA_SIGN),
                   ]), critical=False)
                   .sign(private_key=ca_sk, algorithm=None))

        # 落盘
        ca_cert_pem = ca_cert.public_bytes(_s.Encoding.PEM)
        ee_cert_pem = ee_cert.public_bytes(_s.Encoding.PEM)
        ee_key_pem  = ee_sk.private_bytes(_s.Encoding.PEM, _s.PrivateFormat.PKCS8, _s.NoEncryption())
        ca_key_pem  = ca_sk.private_bytes(_s.Encoding.PEM, _s.PrivateFormat.PKCS8, _s.NoEncryption())
        _C2PA_CA_CERT.write_bytes(ca_cert_pem)
        _C2PA_CA_KEY.write_bytes(ca_key_pem)
        _C2PA_EE_CERT.write_bytes(ee_cert_pem)
        _C2PA_EE_KEY.write_bytes(ee_key_pem)
        chain_pem = (ee_cert_pem + ca_cert_pem).decode("ascii")
        _C2PA_CHAIN_PEM.write_text(chain_pem, "ascii")
        return chain_pem, ee_sk
    except Exception:
        return None


def _build_c2pa_manifest(model: str, profile: str, ai_generated: bool, extra: Optional[dict]) -> str:
    """构建标准 C2PA manifest JSON（含 IPTC trainedAlgorithmicMedia 机器可读标记）。
    claim_generator/softwareAgent/title 均署名为白标品牌，验真时第三方可直接看到出品方。"""
    brand = _brand_generator()
    assertions = [
        {"label": "c2pa.actions", "data": {"actions": [
            {"action": "c2pa.created",
             "digitalSourceType":
                "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"
                if ai_generated else
                "http://cv.iptc.org/newscodes/digitalsourcetype/digitalCapture",
             "softwareAgent": brand}]}},
        {"label": "com.avatarhub.ai_generated",
         "data": {"ai_generated": bool(ai_generated), "model": model,
                  "profile": profile, **(extra or {})}},
    ]
    return json.dumps({
        "claim_generator_info": [{"name": brand, "version": "1.0"}],
        "title": f"{brand} AI Content — {profile or model}" if (profile or model)
                 else f"{brand} AI Content",
        "assertions": assertions,
    }, ensure_ascii=False)


def _make_c2pa_signer(ee_sk, chain_pem: str):
    """从 Ed25519 终端私钥 + 证书链创建 c2pa Signer（from_callback）。"""
    def _sign_cb(data: bytes) -> bytes:
        return ee_sk.sign(data)
    return _C2paSigner.from_callback(_sign_cb, _C2paAlg.ED25519, chain_pem, None)


def embed_c2pa(asset_bytes: bytes, mime_type: str, *,
               model: str = "", profile: str = "", ai_generated: bool = True,
               extra: Optional[dict] = None) -> Optional[bytes]:
    """把标准 C2PA manifest 嵌入给定 MIME 类型的资产字节流。
    返回嵌入后的字节，失败或未装 c2pa-python 时返回 None（调用方应软降级）。
    支持格式：audio/x-wav、video/mp4、image/jpeg、image/png 等（见 c2pa-python 文档）。"""
    if not _HAS_C2PA:
        return None
    try:
        certs = _ensure_c2pa_certs()
        if certs is None:
            return None
        chain_pem, ee_sk = certs
        manifest_json = _build_c2pa_manifest(model, profile, ai_generated, extra)
        signer = _make_c2pa_signer(ee_sk, chain_pem)
        builder = _C2paBuilder(manifest_json)
        src = io.BytesIO(asset_bytes)
        dst = io.BytesIO()
        builder.sign(signer, mime_type, src, dst)
        return dst.getvalue()
    except Exception:
        return None


def read_c2pa(asset_bytes: bytes, mime_type: str) -> Optional[dict]:
    """从资产字节流读取 C2PA manifest store（JSON dict）。未嵌入或出错返回 None。"""
    if not _HAS_C2PA:
        return None
    try:
        r = _C2paReader(mime_type, io.BytesIO(asset_bytes))
        return json.loads(r.json())
    except Exception:
        return None


def c2pa_available() -> bool:
    """是否可用真 C2PA 嵌入（c2pa-python 已装 + 证书可初始化）。"""
    return _HAS_C2PA and _HAS_ED25519


def summarize_c2pa(asset_bytes: bytes, mime_type: str = "video/mp4") -> dict:
    """读取并提炼资产（视频/图片/音频）内嵌的标准 C2PA 凭证为友好结论。
    返回 {has_c2pa, c2pa_supported, claim_generator, ai_generated, digital_source_type,
          signature_issuer, validation_ok, validation_status, active_manifest, title}。
    供 /api/provenance/verify_media：把"换脸/数字人视频"也纳入可验真闭环（音频走水印+凭证仓库，
    视频/图片走标准 C2PA 内嵌，CAI/Verify 工具亦可直读）。"""
    if not _HAS_C2PA:
        return {"has_c2pa": False, "c2pa_supported": False,
                "note": "未安装 c2pa-python，无法读取视频/图片内嵌凭证（音频验真不受影响）。"}
    store = read_c2pa(asset_bytes, mime_type)
    if not store:
        return {"has_c2pa": False, "c2pa_supported": True,
                "note": "未检出 C2PA 凭证（非本系统出品，或导出/转码时凭证已被剥离）。"}
    manifests = store.get("manifests", {}) or {}
    active = store.get("active_manifest")
    m = manifests.get(active) or (next(iter(manifests.values())) if manifests else {})
    ai = None
    dst = ""
    generator = m.get("claim_generator", "") or ""
    if not generator:
        # c2pa-python 通常把署名放在 claim_generator_info 而非顶层 claim_generator
        info = m.get("claim_generator_info") or []
        if isinstance(info, list):
            parts = []
            for it in info:
                if isinstance(it, dict) and it.get("name"):
                    nm = str(it["name"])
                    if it.get("version"):
                        nm += " " + str(it["version"])
                    parts.append(nm)
            generator = " ".join(parts)
    for a in (m.get("assertions", []) or []):
        lbl = a.get("label", "")
        data = a.get("data", {}) or {}
        if "ai_generated" in data:
            ai = data.get("ai_generated")
        if lbl == "c2pa.actions":
            for act in (data.get("actions", []) or []):
                if act.get("digitalSourceType"):
                    dst = act["digitalSourceType"]
                if act.get("softwareAgent") and not generator:
                    generator = act.get("softwareAgent", "")
    # c2pa-python 仅在有问题时给 validation_status；空 = 全部通过。
    vstatus = store.get("validation_status") or store.get("validation_results") or []
    codes = [str(x.get("code", "")) for x in vstatus if isinstance(x, dict)]
    # 区分两类问题：①完整性（被篡改/哈希不符）是硬伤；②证书"untrusted"只是自签链不在公共信任锚，
    # 内容本身完好——绝不能把自签 C2PA 误报成"校验失败/疑似篡改"。
    _HARD = ("mismatch", "invalid", "malformed", "failed", "missing", "tamper", "altered")
    hard = [c for c in codes if any(k in c.lower() for k in _HARD)]
    trust_only = [c for c in codes if "untrust" in c.lower() or "trust" in c.lower()]
    integrity_ok = (len(hard) == 0)               # 内容未被篡改
    trusted = (len(trust_only) == 0 and integrity_ok)  # 证书在公共信任链
    sig = m.get("signature_info", {}) or {}
    return {"has_c2pa": True, "c2pa_supported": True,
            "claim_generator": generator,
            "ai_generated": ai,
            "digital_source_type": dst,
            "signature_issuer": sig.get("issuer", ""),
            "signed_time": sig.get("time", ""),
            "integrity_ok": integrity_ok,         # 内容完整（无篡改类告警）
            "trusted": trusted,                   # 证书是否公共可信（自签为 False，属正常）
            "validation_ok": (len(vstatus) == 0), # 全部通过（含信任锚）
            "validation_status": vstatus[:8],
            "active_manifest": active or "",
            "title": m.get("title", "")}
