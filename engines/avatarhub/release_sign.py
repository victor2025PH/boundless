# -*- coding: utf-8 -*-
"""release_sign.py — 发布清单 Ed25519 签名/验签（供应链防篡改，2026-07-13 P1）。

威胁模型：下载站/CDN/镜像被攻陷 → 篡改 manifest 把某组件 sha256 指向恶意包，
客户端按 sha 校验"通过"却装了坏代码。sha256 只保完整性、不保真实性。
方案：发布方用私钥对 manifest 签名，客户端用【钉死在代码里的公钥】验签才允许安装。

红线设计：
  * 公钥【硬编码在本模块】，随 app 组件/安装包分发——绝不从 manifest 读取
    （否则攻击者连签名带公钥一起换掉就绕过了）。私钥仅在发布机 secrets/，永不分发。
  * 签名覆盖 manifest 全体（除 sig 字段自身）的规范化字节 → 改任何组件 sha 即失效。
  * 平滑迁移 + 防降级：默认"机会性"（有签名必验、无签名放行），但一旦本机见过合法签名，
    即写 no-downgrade 标记，此后拒绝无签名 manifest（挡"删 sig 字段"降级攻击）。
    全部 manifest 上线签名后，把 REQUIRE_SIGNATURE 置 True 转"强制"。
仅依赖 cryptography（客户端运行环境已具备）；缺库时验签降级为"放行 + 告警"，不阻断存量机。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey)
    from cryptography.hazmat.primitives import serialization as _ser
    _HAS = True
except Exception:
    _HAS = False

# ── 钉死的发布公钥【信任集合】（密钥 A，代码完整性）─────────────────────────────
#   2026-07-13 生成，指纹 6b170b9e18a3472a。**轮换用 add-then-swap（P6）**：
#     1) 生成新私钥 → 把新公钥【追加】进本列表（当前+下一把并存）→ 发一版 app 组件让客户端
#        更新到"信任两把"；2) 确认存量客户端都已更新 → 发布切用新私钥签；3) 下一版把旧公钥
#        从列表移除。验签"任一钉死公钥通过即通过"，故换钥窗口内新旧签名都被接受，零断更。
#   列表首项=当前签名用的公钥（sign 侧据私钥文件定；此处仅验签集合）。
PINNED_PUBKEYS = [
    """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAZ4tgtCPVgclFEeKU1rTKsnXKCmHGnQx7UdJZH/U6TZ4=
-----END PUBLIC KEY-----
""",
]
# 向后兼容别名（旧代码/文档引用单钥名）
PINNED_PUBKEY_PEM = PINNED_PUBKEYS[0]

# 全部发布 manifest 均已签名后置 True → 无签名一律拒绝（最强）。迁移期保持 False。
REQUIRE_SIGNATURE = False

# ── 放量控制公钥【信任集合】（密钥 B，2026-07-13 P5）：与代码密钥 A【分离】────────────
#   密钥 A 签 manifest=代码完整性，私钥只在构建机，绝不上互联网。
#   密钥 B 签 rollout_control.json=运行时放量控制（halt/紧急百分比），私钥可放下载站 VPS——
#   即使 VPS 被攻陷，攻击者拿密钥 B 也只能"停更新/改放量比例"(可用性 DoS，halt 是 fail-safe
#   =只会停更不会推坏码)，**无法伪造代码更新**。指纹 2472a94637020a5f。轮换同 add-then-swap。
PINNED_CONTROL_PUBKEYS = [
    # 2026-07-13 P6 轮换演练完成：现役 fp bc7be05fd9d7076d（旧钥 2472a946… 已退役移除）。
    """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEADkFpAlK/Fy0xD3BvCaZno4mymivap9Y80YWSefXDmIA=
-----END PUBLIC KEY-----
""",
]
PINNED_CONTROL_PUBKEY_PEM = PINNED_CONTROL_PUBKEYS[0]

_SK_FILE = Path(__file__).resolve().parent / "secrets" / "release_sign_ed25519_sk.pem"
_CTRL_SK_FILE = Path(__file__).resolve().parent / "secrets" / "rollout_control_ed25519_sk.pem"


def _canonical(manifest: dict) -> bytes:
    """规范化字节（排除 sig 字段自身）：与签名/验签两侧严格一致。"""
    m = {k: v for k, v in manifest.items() if k != "sig"}
    return json.dumps(m, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _pubkey_fp(pem: str) -> str:
    if not _HAS:
        return ""
    try:
        pk = _ser.load_pem_public_key(pem.encode("ascii"))
        raw = pk.public_bytes(_ser.Encoding.Raw, _ser.PublicFormat.Raw)
        return hashlib.sha256(raw).hexdigest()[:16]
    except Exception:
        return ""


def _sk_fp(sk) -> str:
    """从私钥对象取其公钥指纹——签名时据【实际签名私钥】标 key_fp，轮换期不会标错。"""
    try:
        raw = sk.public_key().public_bytes(_ser.Encoding.Raw, _ser.PublicFormat.Raw)
        return hashlib.sha256(raw).hexdigest()[:16]
    except Exception:
        return ""


# ── 发布侧：签名 ─────────────────────────────────────────────────────
def sign_manifest_dict(manifest: dict) -> dict:
    """就地给 manifest 加 sig 字段并返回。需私钥 + cryptography。"""
    if not _HAS:
        raise RuntimeError("需 cryptography 才能签名")
    if not _SK_FILE.exists():
        raise RuntimeError(f"发布私钥缺失：{_SK_FILE}")
    sk = _ser.load_pem_private_key(_SK_FILE.read_bytes(), password=None)
    manifest.pop("sig", None)
    value = sk.sign(_canonical(manifest)).hex()
    manifest["sig"] = {"alg": "Ed25519", "key_fp": _sk_fp(sk), "value": value}
    return manifest


def sign_manifest_file(path: str | Path) -> str:
    p = Path(path)
    m = json.loads(p.read_text(encoding="utf-8"))
    sign_manifest_dict(m)
    p.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    return m["sig"]["key_fp"]


# ── 客户端：验签 + 防降级 ─────────────────────────────────────────────
def _state_file() -> Path:
    return Path(__file__).resolve().parent / "runtime" / "release_sig_state.json"


def _seen_signed() -> bool:
    try:
        return bool(json.loads(_state_file().read_text(encoding="utf-8")).get("seen_signed"))
    except Exception:
        return False


def _mark_seen_signed():
    try:
        f = _state_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"seen_signed": True}), encoding="utf-8")
    except Exception:
        pass


def verify_manifest(manifest: dict) -> tuple[bool, str]:
    """返回 (是否允许安装, 说明)。策略见模块 docstring。"""
    sig = manifest.get("sig") or {}
    has_sig = bool(sig.get("value"))
    if not has_sig:
        if REQUIRE_SIGNATURE:
            return False, "拒绝：manifest 无签名（当前为强制验签模式）"
        if _seen_signed():
            return False, "拒绝：manifest 无签名，但本机曾见过签名版（防降级：疑似被剥离签名）"
        return True, "放行：manifest 无签名（迁移期机会性验签；本机尚未见过签名版）"
    if not _HAS:
        return True, "放行：本机无 cryptography 无法验签（存量环境兼容；建议升级）"
    # 轮换友好：对信任集合里【任一】公钥验过即通过（换钥窗口内新旧签名都接受）。
    data = _canonical(manifest)
    val = bytes.fromhex(sig["value"])
    for pem in PINNED_PUBKEYS:
        try:
            _ser.load_pem_public_key(pem.encode("ascii")).verify(val, data)
            _mark_seen_signed()
            return True, f"验签通过（Ed25519·公钥 {_pubkey_fp(pem)}，信任集合 {len(PINNED_PUBKEYS)} 把）"
        except Exception:
            continue
    return False, "拒绝：签名验证失败（信任集合无一匹配）——manifest 可能被篡改或用了未信任的密钥"


# ── 放量控制通道（密钥 B）：签/验 rollout_control.json ────────────────────────────
def _canonical_control(ctrl: dict) -> bytes:
    c = {k: v for k, v in ctrl.items() if k != "sig"}
    return json.dumps(c, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_control_dict(ctrl: dict) -> dict:
    """用密钥 B 给放量控制消息签名（VPS/构建机均可，私钥 B 允许在 VPS）。就地加 sig 返回。"""
    if not _HAS:
        raise RuntimeError("需 cryptography 才能签名")
    if not _CTRL_SK_FILE.exists():
        raise RuntimeError(f"放量控制私钥缺失：{_CTRL_SK_FILE}")
    sk = _ser.load_pem_private_key(_CTRL_SK_FILE.read_bytes(), password=None)
    ctrl.pop("sig", None)
    ctrl["sig"] = {"alg": "Ed25519", "key_fp": _sk_fp(sk),
                   "value": sk.sign(_canonical_control(ctrl)).hex()}
    return ctrl


def verify_control(ctrl: dict) -> bool:
    """客户端用钉死的公钥 B【信任集合】验放量控制消息（任一通过即通过，支持轮换）。
    无签名/验不过 → False（客户端应忽略该控制、按 manifest 正常放量）。
    过期控制（expires_at 已过）→ 也视为无效（防遗忘的 halt 永久冻结，见 P6 TTL）。"""
    sig = (ctrl or {}).get("sig") or {}
    if not sig.get("value") or not _HAS:
        return False
    exp = ctrl.get("expires_at")
    if exp is not None:
        try:
            if float(exp) > 0 and time.time() > float(exp):
                return False        # 已过期：等同无控制（halt 自动失效）
        except Exception:
            pass
    data = _canonical_control(ctrl)
    val = bytes.fromhex(sig["value"])
    for pem in PINNED_CONTROL_PUBKEYS:
        try:
            _ser.load_pem_public_key(pem.encode("ascii")).verify(val, data)
            return True
        except Exception:
            continue
    return False


def control_fp() -> str:
    return _pubkey_fp(PINNED_CONTROL_PUBKEY_PEM)


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "sign":
        for mp in sys.argv[2:]:
            fp = sign_manifest_file(mp)
            print(f"[sign] {mp} → 已签名（公钥指纹 {fp}）")
    elif len(sys.argv) >= 3 and sys.argv[1] == "verify":
        m = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        ok, why = verify_manifest(m)
        print(f"[verify] ok={ok} {why}")
        sys.exit(0 if ok else 1)
    else:
        print("用法: python release_sign.py sign <manifest...> | verify <manifest>")
        print("本机公钥指纹:", _pubkey_fp(PINNED_PUBKEY_PEM))
