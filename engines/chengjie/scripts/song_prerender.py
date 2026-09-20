# -*- coding: utf-8 -*-
"""清唱备货批渲 CLI（2026-08-22 P0）——模板干声 × 人设 → hub 翻唱(dry_vocal) → 备货。

用法（引擎根运行；夜间低峰跑，白天 176 VRAM 高水位别凑热闹）：

    python scripts/song_prerender.py                    # 全实例根 × 全映射人设 × 全模板
    python scripts/song_prerender.py --persona lin_xiaoyu --template pd_twinkle
    python scripts/song_prerender.py --dry-run          # 只打印计划
    python scripts/song_prerender.py --force            # 忽略新鲜度全部重渲

行为契约：
- 数据根按 scripts/_data_root 契约解析（--data-root → AITR_DATA_ROOT → 实例发现 → 引擎根）；
- 人设→hub 档映射复用 ``avatar_voice.hub_fish.profile_map``（TTS 同一张表，单一事实源）；
- 新鲜度三键：模板源音频 sha1 / hub 侧参考音 sha1（换声自动重渲，防「换声后发旧唱段」）
  / 产物存在且尺寸达标——任一失配即重渲；
- **同档复用**：同一 hub 档 × 同一模板只烧一次 GPU，其余人设字节复制（多人设共声省 GPU）；
- 产物四道验证内建（singing_client：Content-Type/magic/尺寸 + 本地 wav 头解析）；
  有 ffmpeg 则转 ogg/opus 48k mono（TG 语音消息形态），无则保留 wav（警告不阻断）；
- hub 声纹分（clone_score）逐条记录进 meta ——P0 只观测不设地板（唱歌声纹分对
  campplus 系统性偏低，地板待样本标定；人耳裁决仍是首批放行的最终闸）。

失败语义：单条失败继续批（summary 点名），任何失败退出码 1；hub 不可达退出码 2。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from scripts._data_root import load_merged_config, resolve_data_roots  # noqa: E402
from src.ai.singing_client import (  # noqa: E402
    SingingArtifactError, SingingClient, sniff_audio, validate_audio_bytes,
    wav_info,
)
from src.companion.song_stock import (  # noqa: E402
    SongTemplate, load_song_manifest, prepare_dry_vocal, resolve_singing_cfg,
    stock_root, templates_dir,
)

try:  # Windows 控制台 GBK 防乱码（夜间任务日志走重定向文件同样受益）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def ffmpeg_to_ogg(wav_bytes: bytes) -> Optional[bytes]:
    """wav → ogg/opus 48k mono（TG 语音消息形态）。无 ffmpeg/失败 → None。"""
    exe = shutil.which("ffmpeg")
    if not exe:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="songpr_") as td:
            src = Path(td) / "in.wav"
            dst = Path(td) / "out.ogg"
            src.write_bytes(wav_bytes)
            r = subprocess.run(
                [exe, "-y", "-loglevel", "error", "-i", str(src),
                 "-c:a", "libopus", "-b:a", "48k", "-ar", "48000", "-ac", "1",
                 str(dst)],
                capture_output=True, timeout=120)
            if r.returncode != 0 or not dst.is_file():
                log(f"  ffmpeg 转码失败 rc={r.returncode} {r.stderr[:200]!r}")
                return None
            out = dst.read_bytes()
            return out if sniff_audio(out) == "ogg" else None
    except Exception as e:  # noqa: BLE001
        log(f"  ffmpeg 异常: {e}")
        return None


def wait_engine_online(client: SingingClient, budget_sec: float) -> bool:
    """等唱歌引擎上线；顺手试编排器拉起（契约未钉死，逐形试探全软失败）。"""
    try:
        h = client.song_health()
        if h.get("online"):
            return True
    except Exception as e:  # noqa: BLE001
        log(f"song/health 不可达: {e}")
        return False
    log("唱歌引擎离线，尝试经编排器拉起（软失败）…")
    # 契约实测（2026-08-22）：ensure/start 走**查询参数**（?name=），非 JSON body
    # （JSON body 会 422）。ensure 幂等且自带 wait_s 服务端等待。
    for path in (
        "/api/services/ensure?name=singing&wait_s=30",
        "/api/engine/start?name=singing",
    ):
        try:
            r = client._json("POST", path, None, timeout=90)
            log(f"  POST {path} -> {str(r)[:160]}")
            break
        except Exception as e:  # noqa: BLE001
            log(f"  POST {path} -> {e}")
    t0 = time.monotonic()
    while time.monotonic() - t0 < budget_sec:
        time.sleep(10)
        try:
            if client.song_health().get("online"):
                log(f"引擎上线（等待 {time.monotonic() - t0:.0f}s）")
                return True
        except Exception:
            pass
    return False


def load_meta(p: Path) -> Dict[str, Any]:
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def stock_fresh(sdir: Path, tmpl: SongTemplate, src_sha: str,
                hub_ref_sha: str) -> bool:
    """备货新鲜判定：产物在 + meta 三键齐同（源模板/hub 参考音都没换）。"""
    meta = load_meta(sdir / f"{tmpl.id}.json")
    if not meta:
        return False
    audio = None
    for ext in (".ogg", ".wav"):
        cand = sdir / f"{tmpl.id}{ext}"
        if cand.is_file() and cand.stat().st_size >= 40_000:
            audio = cand
            break
    if audio is None:
        return False
    if str(meta.get("src_sha1") or "") != src_sha:
        return False
    # hub 侧参考音指纹：本轮取不到（''）不判陈旧（网络软失败别把好备货全废了）；
    # 取到了且与登记不一致 → 换声了，必须重渲。
    reg = str(meta.get("hub_ref_sha1") or "")
    if hub_ref_sha and reg and reg != hub_ref_sha:
        return False
    return True


def render_one(client: SingingClient, tmpl: SongTemplate, src_bytes: bytes,
               profile: str, *, quality: str, budget_sec: float) -> Dict[str, Any]:
    """单条渲染：提交 dry_vocal 翻唱 → 轮询 → 取产物过闸 → 返回 result dict。"""
    tid = client.submit_cover(
        src_bytes, profile, pitch="auto", quality=quality,
        dry_vocal=True, filename=f"{tmpl.id}.wav")
    log(f"  提交 tid={tid} profile={profile}")
    task = client.poll_task(tid, budget_sec=budget_sec)
    if str(task.get("status")) != "done":
        raise RuntimeError(f"任务未完成: status={task.get('status')} "
                           f"detail={str(task.get('detail'))[:160]}")
    data, kind = client.fetch_result_audio(task)
    sim = task.get("similarity")
    hub_cos = None
    if isinstance(sim, dict) and isinstance(sim.get("cosine"), (int, float)):
        hub_cos = float(sim["cosine"])
    return {"audio": data, "kind": kind, "tid": tid, "hub_similarity": hub_cos}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-root", default="", help="显式数据根（默认按契约解析）")
    ap.add_argument("--persona", action="append", default=[],
                    help="只渲这些人设（可多次；默认=profile_map 全部）")
    ap.add_argument("--template", action="append", default=[],
                    help="只渲这些模板 id（可多次；默认=manifest 全部 enabled）")
    ap.add_argument("--hub", default="", help="覆写 hub 网关（默认读 hub_fish.base_url）")
    ap.add_argument("--quality", default="standard", choices=["standard", "turbo"])
    ap.add_argument("--force", action="store_true", help="忽略新鲜度全部重渲")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划不渲染")
    ap.add_argument("--task-budget", type=float, default=900.0,
                    help="单任务轮询预算秒（默认 900）")
    ap.add_argument("--engine-wait", type=float, default=300.0,
                    help="引擎冷启等待预算秒（默认 300）")
    args = ap.parse_args()

    roots = resolve_data_roots(args.data_root)
    any_fail = False
    for root in roots:
        log(f"== 数据根 {root} ==")
        cfg = load_merged_config(root)
        scfg = resolve_singing_cfg(cfg)
        hf = ((cfg.get("avatar_voice") or {}).get("hub_fish") or {})
        base_url = str(args.hub or hf.get("base_url") or "").strip()
        pmap_raw = hf.get("profile_map")
        pmap: Dict[str, str] = (
            {str(k): str(v) for k, v in pmap_raw.items()}
            if isinstance(pmap_raw, dict) else {})
        if not base_url or not pmap:
            log("  跳过：hub_fish.base_url / profile_map 未配置")
            continue
        tdir = templates_dir(scfg, root)
        templates = [t for t in load_song_manifest(tdir) if t.enabled]
        if args.template:
            want = set(args.template)
            templates = [t for t in templates if t.id in want]
        if not templates:
            log(f"  跳过：模板清单为空（{tdir / 'manifest.json'}）")
            continue
        personas = [p for p in pmap if not args.persona or p in set(args.persona)]
        if not personas:
            log("  跳过：无匹配人设")
            continue
        log(f"  计划：{len(personas)} 人设 × {len(templates)} 模板 → "
            f"{stock_root(scfg, root)}")
        if args.dry_run:
            for pid in personas:
                for t in templates:
                    log(f"    [plan] {pid} × {t.id}（{t.title}）")
            continue

        client = SingingClient(base_url, timeout_sec=120.0)
        if not wait_engine_online(client, args.engine_wait):
            log("  hub 唱歌引擎不可达/未上线——本根整批跳过（exit 2）")
            return 2

        # hub 侧参考音指纹（每档一次）+ 模板源字节（每模板一次）
        ref_sha: Dict[str, str] = {}
        for pid in personas:
            prof = pmap.get(pid) or ""
            if prof and prof not in ref_sha:
                ref_sha[prof] = client.profile_ref_sha1(prof)
        src_cache: Dict[str, bytes] = {}
        rendered: Dict[str, Dict[str, Any]] = {}   # (profile,tmpl,src_sha) → result
        n_done = n_skip = n_fail = 0
        for tmpl in templates:
            sp = tmpl.src_path(tdir)
            if not sp.is_file():
                log(f"  [fail] 模板源缺失 {tmpl.id}: {sp}")
                n_fail += 1
                continue
            raw_src = src_cache.setdefault(tmpl.id, sp.read_bytes())
            try:
                validate_audio_bytes(raw_src, "")
            except SingingArtifactError as e:
                log(f"  [fail] 模板源不是有效音频 {tmpl.id}: {e}")
                n_fail += 1
                continue
            src_bytes, prep = prepare_dry_vocal(raw_src, lyrics=tmpl.lyrics)
            if prep.get("ok"):
                log(f"  模板窗 {tmpl.id}: {prep.get('end_t')}s/"
                    f"{prep.get('src_dur')}s reason={prep.get('reason')} "
                    f"gain=x{prep.get('gain')}")
            else:
                log(f"  ⚠ 模板窗跳过 {tmpl.id}: {prep.get('error') or prep.get('reason')}")
                src_bytes = raw_src
            src_sha = sha1_bytes(src_bytes)
            for pid in personas:
                prof = pmap.get(pid) or ""
                if not prof:
                    continue
                sdir = stock_root(scfg, root) / pid / "songs"
                if not args.force and stock_fresh(
                        sdir, tmpl, src_sha, ref_sha.get(prof, "")):
                    n_skip += 1
                    continue
                reuse_key = f"{prof}|{tmpl.id}|{src_sha}"
                try:
                    if reuse_key in rendered:
                        res = rendered[reuse_key]
                        log(f"  [reuse] {pid} × {tmpl.id} ← 同档 {prof} 本轮成品")
                    else:
                        log(f"  [render] {pid} × {tmpl.id}（{tmpl.title}）…")
                        res = render_one(
                            client, tmpl, src_bytes, prof,
                            quality=args.quality, budget_sec=args.task_budget)
                        res["clone_score"] = client.clone_score(prof, res["audio"])
                        rendered[reuse_key] = res
                    audio, kind = res["audio"], res["kind"]
                    if kind == "wav":
                        audio2, prep2 = prepare_dry_vocal(audio, lyrics=tmpl.lyrics)
                        if prep2.get("ok"):
                            audio = audio2
                            res["audio"] = audio2
                    sr, dur = wav_info(audio) if kind == "wav" else (0, 0.0)
                    ogg = ffmpeg_to_ogg(audio) if kind == "wav" else None
                    out_bytes = ogg if ogg is not None else audio
                    out_ext = ".ogg" if ogg is not None else f".{kind}"
                    if ogg is None and kind == "wav":
                        log("  ⚠ 无 ffmpeg/转码失败，保留 wav（语音消息形态欠佳）")
                    sdir.mkdir(parents=True, exist_ok=True)
                    # 清旧扩展名残留（wav→ogg 升级后不留双份）
                    for old_ext in (".ogg", ".wav", ".mp3"):
                        old = sdir / f"{tmpl.id}{old_ext}"
                        if old_ext != out_ext and old.is_file():
                            old.unlink(missing_ok=True)
                    (sdir / f"{tmpl.id}{out_ext}").write_bytes(out_bytes)
                    (sdir / f"{tmpl.id}.json").write_text(json.dumps({
                        "template_id": tmpl.id, "title": tmpl.title,
                        "lyrics": tmpl.lyrics, "source": tmpl.source,
                        "src_sha1": src_sha, "hub_profile": prof,
                        "hub_ref_sha1": ref_sha.get(prof, ""),
                        "format": out_ext.lstrip("."),
                        "sample_rate": sr, "duration_sec": round(dur, 1),
                        "hub_similarity": res.get("hub_similarity"),
                        "clone_score": res.get("clone_score"),
                        "hub_tid": res.get("tid"),
                        "rendered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "pitch": "auto", "quality": args.quality,
                    }, ensure_ascii=False, indent=1), encoding="utf-8")
                    n_done += 1
                    log(f"  [ok] {pid} × {tmpl.id} → {tmpl.id}{out_ext} "
                        f"({len(out_bytes) // 1024}KB, hub_cos="
                        f"{res.get('hub_similarity')}, clone={res.get('clone_score')})")
                except Exception as e:  # noqa: BLE001
                    n_fail += 1
                    log(f"  [fail] {pid} × {tmpl.id}: {e}")
        log(f"== 根 {root.name} 完成：rendered={n_done} skip(fresh)={n_skip} "
            f"fail={n_fail} ==")
        if n_fail:
            any_fail = True
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
