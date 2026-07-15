# -*- coding: utf-8 -*-
"""角色配置包导出/导入：profile + 预切参考 + KB + 音质元数据（ZIP）。"""
from __future__ import annotations

import base64
import glob
import io
import json
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Awaitable

PACKAGE_FORMAT = "avatarhub_profile_package"
PACKAGE_VERSION = 1

# ── 导出件静置/传输加密（闭合"导出=明文生物特征"缺口）──────────────────────
# 配置包本质是 ZIP（明文存有真人声纹 active_ref.wav 与人脸 face_b64）。开启后整包
# 以 Fernet(AES128-CBC+HMAC) 加密，外覆一个自描述信封，导入端按魔数自动识别：
#   信封 = _ENC_MAGIC(10B) + kind(1B: 'K'本机密钥 / 'P'口令派生) + [P: salt(16B)] + token
# 明文 ZIP（PK\x03\x04 开头）保持原样可导入 → 向后兼容。
_ENC_MAGIC = b"AHPKGENC1\n"
_PBKDF2_ITERS = 200_000


def _fernet_from_key(key):
    from cryptography.fernet import Fernet
    return Fernet(key.encode("ascii") if isinstance(key, str) else key)


def _fernet_from_password(password: str, salt: bytes):
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=_PBKDF2_ITERS)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8"))))


def encryption_kind(data: bytes) -> str:
    """'' = 明文 ZIP；'password' = 口令加密（异机导入要口令）；'key' = 本机密钥加密（黄金包）。"""
    if not is_encrypted_package(data):
        return ""
    kind = data[len(_ENC_MAGIC):len(_ENC_MAGIC) + 1]
    return "password" if kind == b"P" else "key"


def is_encrypted_package(data: bytes) -> bool:
    return bool(data) and data[:len(_ENC_MAGIC)] == _ENC_MAGIC


def encrypt_package(zip_bytes: bytes, *, key=None, password: str = "") -> bytes:
    """把明文 ZIP 包成加密信封；key/password 均空时原样返回（明文兼容）。
    password 优先（便携：异机凭口令导入）；否则用本机 key（黄金包静置加密）。"""
    if not (key or password):
        return zip_bytes
    if password:
        import os as _os
        salt = _os.urandom(16)
        token = _fernet_from_password(password, salt).encrypt(zip_bytes)
        return _ENC_MAGIC + b"P" + salt + token
    token = _fernet_from_key(key).encrypt(zip_bytes)
    return _ENC_MAGIC + b"K" + token


def decrypt_package(data: bytes, *, key=None, password: str = "") -> bytes:
    """识别信封并解密；明文 ZIP 原样返回。缺密钥/口令或不匹配抛 ValueError（失败关闭）。"""
    if not is_encrypted_package(data):
        return data
    body = data[len(_ENC_MAGIC):]
    kind, body = body[:1], body[1:]
    if kind == b"P":
        salt, token = body[:16], body[16:]
        if not password:
            raise ValueError("该配置包为口令加密，请提供导入口令")
        f = _fernet_from_password(password, salt)
    elif kind == b"K":
        token = body
        if not key:
            raise ValueError("该配置包为密钥加密，需本机角色密钥（secrets/profile_key.key 或 AVATARHUB_SECRET_KEY）")
        f = _fernet_from_key(key)
    else:
        raise ValueError("未知的加密配置包类型")
    try:
        return f.decrypt(token)
    except ValueError:
        raise
    except Exception:
        raise ValueError("配置包解密失败：口令/密钥不匹配或文件已损坏")

_EXPORT_KEYS = (
    "description", "voice_name", "hair_style", "tts_engine", "vc_engine",
    "lipsync_engine", "fish_tts_params", "probe_sentences", "system_prompt",
    "filler_phrases", "opener_phrases", "quality_axes", "rvc_model",
    "rvc_settings", "rvc_strict_mode", "voice_quality",
)

_SEGMENT_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff\-\.]+\.wav$", re.UNICODE)


def _segment_dir_from_glob(pattern: str) -> Path | None:
    if not pattern:
        return None
    parent = Path(pattern).parent
    return parent if str(parent) != pattern else None


def _active_segment_name(profile: dict, segment_paths: list[Path]) -> str:
    cur = profile.get("voice_b64", "")
    if not cur:
        return ""
    for fp in segment_paths:
        try:
            b64 = base64.b64encode(fp.read_bytes()).decode()
            if b64 == cur:
                return fp.name
        except OSError:
            continue
    return "active_ref.wav"


def _slim_fish_refs(refs: list) -> list:
    out = []
    for r in refs or []:
        if not isinstance(r, dict):
            continue
        item = {k: v for k, v in r.items() if k != "voice_b64"}
        if r.get("_src"):
            item["voice_file"] = f"segments/{r['_src']}"
        out.append(item)
    return out


def build_profile_package(
    name: str,
    profile: dict,
    *,
    segment_glob: str = "",
    kb_text: str = "",
    kb_chunks: list | None = None,
    include_face: bool = False,
    include_rvc_model: bool = False,
    rvc_resolve_pth=None,
    max_segments: int = 24,
    encrypt_key=None,
    encrypt_password: str = "",
) -> bytes:
    """打包角色配置为 ZIP（音频走文件，避免 JSON 内嵌巨型 base64）。
    include_rvc_model=True 时把绑定的 .pth 一并打入 rvc/ 目录（迁移机器必需，约 +55MB）。
    若给 encrypt_key 或 encrypt_password，则对整包加密（生物特征不落明文）。"""
    seg_paths = sorted(Path(p) for p in glob.glob(segment_glob)) if segment_glob else []
    seg_paths = [p for p in seg_paths if p.is_file()][:max(1, min(max_segments, 48))]
    active_seg = _active_segment_name(profile, seg_paths)

    prof_out = {k: profile.get(k) for k in _EXPORT_KEYS if k in profile}
    prof_out["fish_refs"] = _slim_fish_refs(profile.get("fish_refs") or [])
    prof_out["active_voice_file"] = (
        f"segments/{active_seg}" if active_seg and active_seg != "active_ref.wav"
        else "active_ref.wav"
    )
    if include_face and profile.get("face_b64"):
        prof_out["face_b64"] = profile["face_b64"]
        if profile.get("thumbnail_b64"):
            prof_out["thumbnail_b64"] = profile["thumbnail_b64"]

    manifest = {
        "format": PACKAGE_FORMAT,
        "format_version": PACKAGE_VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "profile_name": name,
        "active_segment": active_seg or "active_ref.wav",
        "quality_axes": profile.get("quality_axes") or {},
        "segment_glob": Path(segment_glob).name if segment_glob else "",
        "segment_count": len(seg_paths),
        "has_kb": bool(kb_text or kb_chunks),
        "include_face": include_face,
    }
    rvc_rel = (profile.get("rvc_model") or "").strip()
    rvc_zip_path = ""
    rvc_bytes = None
    if include_rvc_model and rvc_rel and rvc_resolve_pth:
        try:
            pth_abs = Path(rvc_resolve_pth(rvc_rel))
            if pth_abs.is_file():
                rvc_zip_path = f"rvc/{rvc_rel.replace(chr(92), '/')}"
                rvc_bytes = pth_abs.read_bytes()
                manifest["has_rvc_model"] = True
                manifest["rvc_model_file"] = rvc_zip_path
        except Exception:
            pass

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("profile.json", json.dumps(prof_out, ensure_ascii=False, indent=2))
        if kb_text:
            zf.writestr("kb.txt", kb_text)
        if kb_chunks:
            zf.writestr("kb_chunks.json",
                        json.dumps(kb_chunks, ensure_ascii=False, indent=2))
        wrote_active = False
        for fp in seg_paths:
            zf.write(fp, f"segments/{fp.name}")
            if fp.name == active_seg:
                wrote_active = True
        if not wrote_active and profile.get("voice_b64"):
            try:
                raw = base64.b64decode(profile["voice_b64"])
                zf.writestr("active_ref.wav", raw)
            except Exception:
                pass
        if rvc_bytes and rvc_zip_path:
            zf.writestr(rvc_zip_path, rvc_bytes)
    buf.seek(0)
    return encrypt_package(buf.getvalue(), key=encrypt_key, password=encrypt_password)


def parse_profile_package(data: bytes, *, key=None, password: str = "") -> dict:
    """解析配置包；若为加密信封先解密（key=本机密钥 / password=口令），再按 ZIP 解析。"""
    if not data or len(data) < 32:
        raise ValueError("配置包为空或过小")
    if len(data) > 210 * 1024 * 1024:
        raise ValueError("配置包超过大小限制")
    data = decrypt_package(data, key=key, password=password)   # 明文 ZIP 原样返回
    if len(data) > 200 * 1024 * 1024:
        raise ValueError("配置包超过 200MB 限制")

    with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
        names = set(zf.namelist())
        if "manifest.json" not in names or "profile.json" not in names:
            raise ValueError("缺少 manifest.json 或 profile.json")
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        if manifest.get("format") != PACKAGE_FORMAT:
            raise ValueError(f"不支持的配置包格式: {manifest.get('format')}")
        if int(manifest.get("format_version") or 0) > PACKAGE_VERSION:
            raise ValueError("配置包版本过新，请升级 Hub")

        profile = json.loads(zf.read("profile.json").decode("utf-8"))
        kb_text = ""
        if "kb.txt" in names:
            kb_text = zf.read("kb.txt").decode("utf-8", errors="replace")
        kb_chunks = []
        if "kb_chunks.json" in names:
            kb_chunks = json.loads(zf.read("kb_chunks.json").decode("utf-8"))

        segments: dict[str, bytes] = {}
        for n in names:
            if n.startswith("segments/") and not n.endswith("/"):
                seg_name = Path(n).name
                if _SEGMENT_NAME_RE.fullmatch(seg_name):
                    segments[seg_name] = zf.read(n)
        if "active_ref.wav" in names:
            segments.setdefault("active_ref.wav", zf.read("active_ref.wav"))

        rvc_files: dict[str, bytes] = {}
        for n in names:
            if n.startswith("rvc/") and not n.endswith("/"):
                rvc_files[n] = zf.read(n)

    return {
        "manifest": manifest,
        "profile": profile,
        "segments": segments,
        "rvc_files": rvc_files,
        "kb_text": kb_text,
        "kb_chunks": kb_chunks,
    }


def write_segments(segments: dict[str, bytes], target_dir: Path) -> list[str]:
    """将配置包内参考段写入声音包目录。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name, raw in segments.items():
        if name == "active_ref.wav":
            continue
        if not _SEGMENT_NAME_RE.fullmatch(name):
            continue
        fp = target_dir / name
        fp.write_bytes(raw)
        written.append(name)
    return written


def materialize_profile(
    parsed: dict,
    profile_name: str,
    *,
    segment_dir: Path | None = None,
) -> dict:
    """从配置包条目重建可落库的 profile dict（含 voice_b64 / fish_refs）。"""
    manifest = parsed["manifest"]
    src = parsed["profile"]
    segments: dict[str, bytes] = parsed["segments"]

    if segment_dir:
        write_segments(segments, segment_dir)

    def _b64_for_file(fname: str) -> str:
        if fname in segments:
            return base64.b64encode(segments[fname]).decode()
        base = Path(fname).name
        if base in segments:
            return base64.b64encode(segments[base]).decode()
        if segment_dir:
            fp = segment_dir / base
            if fp.is_file():
                return base64.b64encode(fp.read_bytes()).decode()
        return ""

    active_file = src.get("active_voice_file") or f"segments/{manifest.get('active_segment', '')}"
    voice_b64 = _b64_for_file(active_file)
    if not voice_b64 and "active_ref.wav" in segments:
        voice_b64 = base64.b64encode(segments["active_ref.wav"]).decode()

    fish_refs = []
    for r in src.get("fish_refs") or []:
        if not isinstance(r, dict):
            continue
        vf = r.get("voice_file") or ""
        b64 = _b64_for_file(vf) if vf else ""
        if not b64:
            continue
        entry = {"voice_b64": b64, "text": r.get("text") or ""}
        src_name = Path(vf).name if vf else ""
        if src_name:
            entry["_src"] = src_name
        fish_refs.append(entry)

    if not fish_refs and voice_b64:
        fish_refs = [{
            "voice_b64": voice_b64,
            "text": (src.get("fish_tts_params") or {}).get("reference_text") or "",
            "_src": manifest.get("active_segment") or "active_ref.wav",
        }]

    out = {k: src[k] for k in _EXPORT_KEYS if k in src}
    out["voice_b64"] = voice_b64
    out["fish_refs"] = fish_refs
    if src.get("face_b64"):
        out["face_b64"] = src["face_b64"]
    if src.get("thumbnail_b64"):
        out["thumbnail_b64"] = src["thumbnail_b64"]
    return out


def install_rvc_from_package(parsed: dict, weights_dir: Path) -> str:
    """从配置包安装变声模型到 weights 目录；返回应写入 profile.rvc_model 的相对 id。"""
    manifest = parsed.get("manifest") or {}
    zip_path = (manifest.get("rvc_model_file") or "").strip()
    if not zip_path:
        return (parsed.get("profile") or {}).get("rvc_model") or ""
    raw = (parsed.get("rvc_files") or {}).get(zip_path)
    if not raw:
        return (parsed.get("profile") or {}).get("rvc_model") or ""
    inner = zip_path[4:] if zip_path.startswith("rvc/") else os.path.basename(zip_path)
    dest = weights_dir / inner.replace("/", os.sep)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    return inner.replace("\\", "/")


def cosine_gate(manifest: dict, *, min_cosine: float) -> dict:
    """静态闸门：包内体检 cosine 不得低于阈值。"""
    axes = manifest.get("quality_axes") or {}
    cos = float(axes.get("cosine") or 0.0)
    if cos <= 0:
        return {"ok": True, "skipped": True, "reason": "包内无 cosine 基线，跳过静态闸门"}
    ok = cos >= min_cosine
    return {
        "ok": ok,
        "cosine": cos,
        "min_cosine": min_cosine,
        "reason": "cosine 达标" if ok else f"包内 cosine {cos:.3f} 低于阈值 {min_cosine:.3f}",
    }


async def import_gate(
    existing: dict | None,
    new_prof: dict,
    manifest: dict,
    *,
    min_cosine: float,
    holdout_gate: Callable[[dict], Awaitable[dict]] | None = None,
    force: bool = False,
) -> dict:
    """导入闸门：静态 cosine + 可选 holdout 对比现有参考。"""
    if force:
        return {"ok": True, "forced": True, "reason": "force 跳过闸门"}

    static = cosine_gate(manifest, min_cosine=min_cosine)
    if not static.get("ok") and not static.get("skipped"):
        return {"ok": False, "static": static, "reason": static.get("reason")}

    holdout = {"ok": True, "skipped": True}
    if existing and existing.get("voice_b64") and new_prof.get("voice_b64"):
        if existing["voice_b64"] == new_prof["voice_b64"]:
            holdout = {"ok": True, "reason": "参考音频未变化"}
        elif holdout_gate:
            cand = {
                "voice_b64": new_prof["voice_b64"],
                "text": (new_prof.get("fish_tts_params") or {}).get("reference_text") or "",
            }
            holdout = await holdout_gate(cand)
            if not holdout.get("ok_to_apply"):
                return {
                    "ok": False,
                    "static": static,
                    "holdout": holdout,
                    "reason": holdout.get("reason", "holdout 未通过"),
                    "can_force": True,
                }

    return {"ok": True, "static": static, "holdout": holdout, "reason": "闸门通过"}
