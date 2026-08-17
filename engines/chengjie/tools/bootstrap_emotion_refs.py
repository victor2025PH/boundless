# -*- coding: utf-8 -*-
"""情绪参考音自举管线（2026-08-03）——零录音补齐 hub emotion_refs。

背景：hub 情感机制＝``profile.emotion_refs = {emotion: [参考录音]}``（命中情绪
标签即改用该情绪参考录音克隆），2026-08-03 实测 51 档全空——链路通着、缺数据。
真人录音排期难，公开情感语料（ESD 等）是**别人的声音**（该机制连音色一起克隆，
直接用=换人）；本管线用**人设自己的底样**自举：

    本地底样(config/voice_refs/<pid>.wav + 逐字稿 sidecar)
      → 7852 CosyVoice3 混合保真模式（emotion + reference_text：音色不漂、情感真，
        Phase10 已验证）× 情绪台词 × N 候选
      → campplus 声纹分质检（vs 底样，≥ min_score 才要；候选择优）
      → 产物落 tmp_emotion_refs/<pid>/<emotion>.wav + manifest
      → [--upload] POST hub /api/emotion_ref（自动切段+落库+回传试听预览），
        预览再过一次声纹分（双重 QC）

默认 **dry-run**（只合成+评分+落盘，不动 hub）；``--upload`` 才写 hub 档。
回滚：对该情绪 POST /api/emotion_ref 空音频即清除（接口语义）。

GPU 纪律：走 AvatarVoiceClient（模块级串行锁，只 HTTP 不本进程载模型）；
单人设约 12 次合成 ≈ 1 分钟，勿在语音高峰批量全跑。

用法：
    python tools/bootstrap_emotion_refs.py --personas mizuki            # 试点 dry-run
    python tools/bootstrap_emotion_refs.py --personas mizuki --upload   # 上传 + 预览
    python tools/bootstrap_emotion_refs.py --personas all [--upload]
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ENGINE))
_MFYS = Path(r"D:/faceX/mfys")

# 情绪台词：与情绪同频的聊天语体短句（18~30 字 ≈ 4~8s 音频，参考音最佳长度带）。
# 刻意不写「哈哈」等笑字（TTS 念出来假，开心靠语气本身）；每句自带语气词/停顿。
EMOTION_SCRIPTS: Dict[str, str] = {
    "gentle":  "嗯……没事的呀，我在这儿陪着你呢，慢慢来，不着急哈。",
    "happy":   "诶你猜怎么着，今天特别顺利，我开心得不行，走路都带风呀！",
    "sad":     "唉……今天有点不想说话，心里闷闷的，你能陪我坐一会儿吗。",
    "excited": "天哪，这也太棒了吧！我现在特别激动，必须马上跟你分享！",
    "calm":    "嗯，我在呢。刚泡了杯茶坐下来，咱们慢慢聊，不急。",
    "serious": "这件事我得认真跟你说，你听我讲完，这个对你真的很重要。",
}
PREVIEW_TEXT = "今天真的很谢谢你呀，陪我聊了这么久，希望咱们以后也一直这样。"


def _load_scorer():
    """campplus 声纹评分器（CPU onnx，复用集群 clone_scorer；缺失返回 None）。"""
    if not _MFYS.is_dir():
        return None
    sys.path.insert(0, str(_MFYS))
    try:
        from clone_scorer import score_similarity
        return score_similarity
    except Exception:
        return None


def _resolve_targets(data_root: Path, personas: str) -> List[Tuple[str, str, Path]]:
    """→ [(persona_id, hub_profile, ref_path)]；按 profile_map ∩ 本地底样存在。"""
    from scripts._data_root import load_merged_config

    cfg = load_merged_config(data_root)
    hf = ((cfg.get("avatar_voice") or {}).get("hub_fish") or {})
    pmap: Dict[str, str] = (hf.get("profile_map")
                            if isinstance(hf.get("profile_map"), dict) else {})
    want = ([p.strip() for p in personas.split(",") if p.strip()]
            if personas != "all" else sorted(pmap.keys()))
    out: List[Tuple[str, str, Path]] = []
    for pid in want:
        ref = data_root / "config" / "voice_refs" / f"{pid}.wav"
        if not ref.is_file():
            print(f"  [skip] {pid}: 本地底样缺失 {ref}")
            continue
        profile = str(pmap.get(pid) or "").strip()
        if not profile:
            print(f"  [skip] {pid}: 不在 hub_fish.profile_map")
            continue
        out.append((pid, profile, ref))
    return out


def _synth_candidates(client, *, script: str, ref_b64: str, ref_text: str,
                      emotion: str, n: int) -> List[bytes]:
    """N 个候选（prosody_variation 采样自带多样性）；单条失败跳过不中断。"""
    outs: List[bytes] = []
    for i in range(max(1, n)):
        try:
            wav = client.tts(
                script, reference_audio_b64=ref_b64, reference_text=ref_text,
                emotion=emotion, prosody_variation=True)
            if wav:
                outs.append(wav)
        except Exception as ex:
            print(f"    候选 {i + 1} 合成失败: {ex}")
    return outs


def _hub_upload(hub_url: str, profile: str, emotion: str, wav: bytes,
                script: str) -> Dict[str, Any]:
    """POST /api/emotion_ref（save=true）→ 响应 dict（含 preview_base64）。"""
    payload = json.dumps({
        "profile": profile, "emotion": emotion,
        "segments": [{"voice_b64": base64.b64encode(wav).decode("ascii"),
                      "text": script}],
        "save": True, "preview_text": PREVIEW_TEXT,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{hub_url.rstrip('/')}/api/emotion_ref", data=payload,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main(argv: Optional[List[str]] = None) -> int:
    if sys.stdout and hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="情绪参考音自举（合成+QC+上传）")
    ap.add_argument("--personas", default="", required=False,
                    help="逗号分隔的人设 id，或 all")
    ap.add_argument("--emotions", default=",".join(EMOTION_SCRIPTS))
    ap.add_argument("--candidates", type=int, default=2)
    ap.add_argument("--min-score", type=float, default=0.70,
                    help="声纹分下限（正常带 0.78~0.86；<0.70=音色漂移拒收）")
    ap.add_argument("--min-match", type=float, default=0.75,
                    help="转写回验下限（念的必须是台词；STT 不可用时自动跳过该闸）")
    ap.add_argument("--upload", action="store_true", help="通过 QC 后上传 hub")
    ap.add_argument("--reuse", action="store_true",
                    help="产物目录已有 <emotion>.wav 则直接复用（重评分不重合成）——"
                         "dry-run 试听后上传不用再烧一遍 GPU")
    ap.add_argument("--data-root", default="")
    ap.add_argument("--out", default="", help="产物目录（缺省 <数据根>/tmp_emotion_refs）")
    args = ap.parse_args(argv)
    if not args.personas:
        ap.error("--personas 必填（人设 id 或 all）")

    from scripts._data_root import load_merged_config, resolve_data_roots
    from src.ai.avatar_voice import AvatarVoiceClient, find_reference_text, load_reference_b64

    root = resolve_data_roots(args.data_root)[0]
    cfg = load_merged_config(root)
    hub_url = str(((cfg.get("avatar_voice") or {}).get("hub_fish") or {})
                  .get("base_url") or "http://192.168.0.176:9000")
    out_dir = Path(args.out) if args.out else root / "tmp_emotion_refs"
    scorer = _load_scorer()
    if scorer is None:
        print("[!] clone_scorer 不可用（D:/faceX/mfys），无法 QC —— 中止（宁缺毋滥）")
        return 1
    client = AvatarVoiceClient({"enabled": True})
    emotions = [e.strip() for e in args.emotions.split(",") if e.strip()]
    bad = [e for e in emotions if e not in EMOTION_SCRIPTS]
    if bad:
        print(f"[!] 无台词的情绪: {bad}（可选: {sorted(EMOTION_SCRIPTS)}）")
        return 1

    targets = _resolve_targets(root, args.personas)
    if not targets:
        print("[!] 无可处理人设")
        return 1
    print(f"=== 情绪参考音自举：{len(targets)} 人设 × {len(emotions)} 情绪 × "
          f"{args.candidates} 候选（min_score={args.min_score}，"
          f"{'上传' if args.upload else 'dry-run'}）===")

    overall_ok = True
    for pid, profile, ref_path in targets:
        print(f"\n--- {pid} → hub「{profile}」 ---")
        try:
            ref_b64 = load_reference_b64(str(ref_path))
            ref_text = find_reference_text(str(ref_path)) or ""
        except Exception as ex:
            print(f"  [!] 底样加载失败: {ex}")
            overall_ok = False
            continue
        pdir = out_dir / pid
        pdir.mkdir(parents=True, exist_ok=True)
        manifest: Dict[str, Any] = {
            "persona": pid, "profile": profile, "ref": str(ref_path),
            "ts": time.time(), "min_score": args.min_score, "emotions": {},
        }
        for emo in emotions:
            script = EMOTION_SCRIPTS[emo]
            _prev = pdir / f"{emo}.wav"
            if args.reuse and _prev.is_file():
                cands = [_prev.read_bytes()]
            else:
                cands = _synth_candidates(
                    client, script=script, ref_b64=ref_b64, ref_text=ref_text,
                    emotion=emo, n=args.candidates)
            # 双闸准入（2026-08-03 乱码事故教训）：① 转写回验——参考音念的必须
            # 就是台词本身（陈旧逐字稿会让模型「续写参考音」，产物是同音色乱码，
            # 声纹分 0.94+ 照样过）；② 声纹分。两闸都过才在候选里按声纹择优。
            scored: List[Tuple[float, float, bytes]] = []   # (声纹, 回验, wav)
            for wav in cands:
                rv = scorer(ref_b64, base64.b64encode(wav).decode("ascii"))
                if not rv.get("ok"):
                    continue
                sim = float(rv.get("similarity") or rv.get("cosine") or 0)
                try:
                    from tools.qc_emotion_refs import text_match_ratio
                    stt = client.stt(wav, language="") or ""
                    match = text_match_ratio(script, stt)
                except Exception:
                    match = -1.0            # STT 不可用：只按声纹（旧行为，如实记 -1）
                scored.append((sim, match, wav))
            scored.sort(key=lambda x: -x[0])
            entry: Dict[str, Any] = {
                "script": script,
                "candidate_scores": [round(s, 4) for s, _, _ in scored],
                "candidate_matches": [round(m, 2) for _, m, _ in scored],
            }
            eligible = [(s, m, w) for s, m, w in scored
                        if s >= args.min_score and (m < 0 or m >= args.min_match)]
            if not eligible:
                top_s = scored[0][0] if scored else 0.0
                top_m = scored[0][1] if scored else 0.0
                print(f"  {emo:<8} 拒收（声纹 {top_s:.3f} / 回验 {top_m:.2f}，"
                      f"门槛 {args.min_score}/{args.min_match}）")
                entry["accepted"] = False
                manifest["emotions"][emo] = entry
                overall_ok = False
                continue
            best_score, best_match, best_wav = eligible[0]
            entry["stt_match"] = round(best_match, 2)
            wav_path = pdir / f"{emo}.wav"
            wav_path.write_bytes(best_wav)
            entry.update({"accepted": True, "score": round(best_score, 4),
                          "file": str(wav_path)})
            line = f"  {emo:<8} 声纹 {best_score:.3f} → {wav_path.name}"
            if args.upload:
                try:
                    rv = _hub_upload(hub_url, profile, emo, best_wav, script)
                    entry["uploaded"] = bool(rv.get("saved"))
                    pv = rv.get("preview_base64") or ""
                    if pv:
                        pv_path = pdir / f"preview_{emo}.wav"
                        pv_path.write_bytes(base64.b64decode(pv))
                        pv_rv = scorer(ref_b64, pv)
                        if pv_rv.get("ok"):
                            entry["preview_score"] = round(
                                float(pv_rv.get("similarity")
                                      or pv_rv.get("cosine") or 0), 4)
                            line += (f"  已上传，预览声纹 "
                                     f"{entry['preview_score']:.3f}")
                except Exception as ex:
                    entry["uploaded"] = False
                    entry["upload_error"] = str(ex)[:120]
                    line += f"  上传失败: {ex}"
                    overall_ok = False
            print(line)
            manifest["emotions"][emo] = entry
        (pdir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  manifest → {pdir / 'manifest.json'}")
    print("\n完成。" + ("" if args.upload else
                     "（dry-run：试听 tmp_emotion_refs/ 后加 --upload 写 hub）"))
    return 0 if overall_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
