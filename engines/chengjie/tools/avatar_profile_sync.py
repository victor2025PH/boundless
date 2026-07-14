# -*- coding: utf-8 -*-
"""人设 → AvatarHub 数字人档案同步（Phase18：face_ref 一源多用打通视频链）。

把本仓的人设资产推成 AvatarHub(.176:9000) 角色档案，让 ``video_autosend``
（数字人口播视频）与图片链共用同一张脸、同一把声：

  - 形象：assets/persona_media/<pid>/face_ref.*   → profile.face_b64
  - 声音：persona.voice_profile.reference_audio_path → profile.voice_b64
  - 档名：persona_id（video_autosend.resolve_avatar_profile 缺省即按 persona_id 找档）

幂等：档案已存在 → PATCH 更新 face/voice/描述；不存在 → POST 创建。
运营在 Studio 换 face_ref 后重跑本工具即可整链换脸（图片链是实时读、视频链走本同步）。

用法（项目根目录）：
    python tools/avatar_profile_sync.py                  # 全部有 face_ref 的人设
    python tools/avatar_profile_sync.py --personas lin_xiaoyu
    python tools/avatar_profile_sync.py --smoke lin_xiaoyu  # 同步后出一段试听口播验证
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

_B64_LIMIT = 5 * 1024 * 1024  # AvatarHub 单字段 5MB 上限（服务端硬校验）
_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}


def _log(m: str) -> None:
    print("[avatar_sync] " + m, flush=True)


def _req(base: str, path: str, method: str = "GET", payload: dict | None = None,
         timeout: float = 60.0) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, {}


def _b64_file(path: Path) -> str:
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    if len(b64) > _B64_LIMIT:
        raise ValueError(f"{path.name} base64 超 5MB（{len(b64)} 字节），请压缩")
    return b64


def find_face_ref(album_root: Path, pid: str) -> Path | None:
    d = album_root / pid
    if not d.is_dir():
        return None
    for p in sorted(d.iterdir()):
        if p.is_file() and p.stem.lower() == "face_ref" and p.suffix.lower() in _IMAGE_EXT:
            return p
    return None


def sync_one(base: str, pid: str, persona: dict, album_root: Path,
             *, lipsync_engine: str = "musetalk",
             tts_engine: str = "fish_speech") -> bool:
    face = find_face_ref(album_root, pid)
    if face is None:
        _log(f"跳过 {pid}：无 face_ref（先跑 persona_photoshoot 或在 Studio 上传）")
        return False
    payload: dict = {
        "name": pid,
        "description": f"{persona.get('name', pid)} / {persona.get('role', '')}"[:80],
        "face_b64": _b64_file(face),
        "lipsync_engine": lipsync_engine,
        # 2026-07-14 实测：.176 默认 TTS(7851)离线、fish_tts(7855)在线——neutral 口播
        # 走默认路由必挂("All connection attempts failed")，显式钉 fish_speech。
        "tts_engine": tts_engine,
    }
    vp = persona.get("voice_profile") or {}
    ref_audio = str(vp.get("reference_audio_path") or "").strip()
    if ref_audio:
        ap = Path(ref_audio)
        if not ap.is_absolute():
            ap = ROOT / ap
        if ap.is_file():
            try:
                payload["voice_b64"] = _b64_file(ap)
            except ValueError as e:
                _log(f"  {pid} 参考音跳过：{e}")
    st, _ = _req(base, f"/profiles/{urllib.request.quote(pid)}")
    if st == 200:
        code, resp = _req(base, f"/profiles/{urllib.request.quote(pid)}",
                          "PATCH", payload)
        action = "更新"
    else:
        code, resp = _req(base, "/profiles", "POST", payload)
        action = "创建"
    ok = code == 200 and (resp.get("ok", True))
    _log(f"{'OK ' if ok else '!! '}{action} {pid}"
         f"（face={face.name}, voice={'有' if 'voice_b64' in payload else '无'}）"
         + ("" if ok else f" code={code} resp={str(resp)[:160]}"))
    return ok


def smoke(base: str, pid: str, out: Path, *, text: str = "嗨，这条是数字人链路测试哦",
          timeout: float = 300.0) -> bool:
    """同步后试出一段口播视频（不发给任何客户，只落本地供人工查看）。"""
    _log(f"smoke: /avatar/speak profile={pid} …（口型合成较慢，最长 {int(timeout)}s）")
    code, resp = _req(base, "/avatar/speak", "POST", {
        "text": text, "profile": pid, "generate_lipsync": True,
        "language": "zh-cn", "emotion": "neutral"}, timeout=timeout)
    b64 = str(resp.get("lipsync_video_b64") or resp.get("video_b64") or "")
    if code != 200 or not b64:
        keys = ",".join(sorted(resp.keys())) if isinstance(resp, dict) else "?"
        _log(f"!! smoke 失败 code={code} 字段=[{keys}]（口型引擎未运行？）")
        return False
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(base64.b64decode(b64))
    _log(f"OK smoke 视频已落 {out}（{out.stat().st_size // 1024} KB）")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://192.168.0.176:9000")
    ap.add_argument("--personas", default="", help="逗号分隔；空=全部有 face_ref 的")
    ap.add_argument("--album-root", default="assets/persona_media")
    ap.add_argument("--smoke", default="", help="同步后对该人设试出一段口播视频")
    args = ap.parse_args()

    profiles = (yaml.safe_load(
        (ROOT / "config" / "profiles_runtime.yaml").read_text(encoding="utf-8"))
        or {}).get("profiles") or {}
    want = [s.strip() for s in args.personas.split(",") if s.strip()] or list(profiles)
    album_root = (ROOT / args.album_root).resolve()

    failed = 0
    for pid in want:
        p = profiles.get(pid)
        if not isinstance(p, dict):
            _log(f"跳过 {pid}：无此人设")
            failed += 1
            continue
        try:
            if not sync_one(args.base, pid, p, album_root):
                failed += 1
        except Exception as e:  # noqa: BLE001
            _log(f"!! {pid} 同步异常：{type(e).__name__} {e}")
            failed += 1
    if args.smoke:
        out = ROOT / "tmp_selfies" / f"smoke_{args.smoke}_{int(time.time())}.mp4"
        if not smoke(args.base, args.smoke.strip(), out):
            failed += 1
    _log(f"完成：{len(want)} 个目标，失败 {failed}")
    return failed


if __name__ == "__main__":
    sys.exit(main())
