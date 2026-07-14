"""AvatarHub 批量预渲染 CLI — 用 Qwen3-TTS(7858) 夜间预合成固定台词。

7858 音色最像但 RTF≈2.8（1 秒音频要 2.8 秒算力），只适合离线批量；在线回复走
7852（见 avatar_voice.AvatarVoiceClient.tts）。本脚本把每个人设的固定台词
（早安/晚安/问候等）批量合成为 OGG/Opus 语音条，落盘到
``assets/voices/<persona>/prerendered/<sha1(text)8>.ogg`` + 同名 ``.txt``（台词原文），
供上层按台词直接复用（零合成延迟）。

用法（在 mur 环境）：
  python -m scripts.avatar_prerender --persona lin_xiaoyu --lines-file config/prerender_lines.txt
  python -m scripts.avatar_prerender --persona lin_xiaoyu --lines "早安呀" "晚安，做个好梦"
  # 默认从 config 读该人设 voice_profile.reference_audio_path；也可 --ref 显式指定
  # 人设换了参考音后旧渲染音色过期 → 加 --force 全量重渲

产物被**在线路径自动复用**（TTSPipeline._try_prerendered：同人设+同台词直接发
预渲染 OGG，零 GPU 零延迟）——键/归一化经 src/ai/voice_prerender.py 单一出口，
渲染与查询两侧保证一致。

服务没起时自动经计划任务 Qwen3TTS_Boot 拉起并等就绪。批量走全局 GPU 串行锁
（与在线 7852 共享一张 3060）——**建议只在夜间低峰跑**。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# 允许 `python scripts/avatar_prerender.py` 直跑（非 -m）时找到 src 包
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Windows GBK 控制台兜底：输出统一 UTF-8（✓/✗ 等符号不再炸 UnicodeEncodeError）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:
    pass


def _load_config() -> dict:
    """读合并后的项目配置（config.yaml + config.local.yaml overlay）。"""
    import yaml

    cfg_path = _ROOT / "config" / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    local = _ROOT / "config" / "config.local.yaml"
    if local.is_file():
        overlay = yaml.safe_load(local.read_text(encoding="utf-8")) or {}

        def _deep_merge(dst: dict, src: dict) -> dict:
            for k, v in src.items():
                if isinstance(v, dict) and isinstance(dst.get(k), dict):
                    _deep_merge(dst[k], v)
                else:
                    dst[k] = v
            return dst

        _deep_merge(data, overlay)
    return data


def _resolve_ref(persona: str, cfg: dict) -> str:
    """人设参考音路径：profiles_runtime / config personas / telegram.voice_reply 逐层找。"""
    try:
        import yaml
        rt = _ROOT / "config" / "profiles_runtime.yaml"
        if rt.is_file():
            profiles = (yaml.safe_load(rt.read_text(encoding="utf-8")) or {}).get(
                "profiles") or {}
            vp = (profiles.get(persona) or {}).get("voice_profile") or {}
            ref = str(vp.get("reference_audio_path") or "").strip()
            if ref:
                return ref
    except Exception:
        pass
    for p in (cfg.get("personas") or {}).get("profiles") or []:
        if isinstance(p, dict) and p.get("id") == persona:
            ref = str((p.get("voice_profile") or {}).get(
                "reference_audio_path") or "").strip()
            if ref:
                return ref
    return str((((cfg.get("telegram") or {}).get("voice_reply") or {}).get(
        "voice_profile") or {}).get("reference_audio_path") or "").strip()


def _collect_avatar_personas(cfg: dict) -> list:
    """--all-personas 的目标收集：voice_profile.backend=avatar_clone 且参考音在盘的人设。

    来源＝profiles_runtime.yaml（运行时人设，权威）∪ config personas.profiles。
    返回 [(persona_id, ref_path)]，按 id 去重（runtime 优先）。
    """
    out: list = []
    seen: set = set()

    def _take(pid: str, vp: dict) -> None:
        if not pid or pid in seen or not isinstance(vp, dict):
            return
        if str(vp.get("backend") or "").strip().lower() != "avatar_clone":
            return
        ref = str(vp.get("reference_audio_path") or "").strip()
        if ref and Path(ref).is_file():
            seen.add(pid)
            out.append((pid, ref))

    try:
        import yaml
        rt = _ROOT / "config" / "profiles_runtime.yaml"
        if rt.is_file():
            profiles = (yaml.safe_load(rt.read_text(encoding="utf-8")) or {}).get(
                "profiles") or {}
            for pid, p in profiles.items():
                if isinstance(p, dict):
                    _take(str(pid), p.get("voice_profile") or {})
    except Exception:
        pass
    for p in (cfg.get("personas") or {}).get("profiles") or []:
        if isinstance(p, dict):
            _take(str(p.get("id") or ""), p.get("voice_profile") or {})
    return out


def ref_fingerprint(ref: str) -> tuple:
    """参考音指纹（路径+大小+mtime）：跨人设共享同一参考音的判定键。"""
    p = Path(ref)
    st = p.stat()
    return (str(p.resolve()), st.st_size, int(st.st_mtime))


def render_persona(
    client, persona: str, ref: str, lines: list, *,
    ref_text: str = "", language: str = "zh", base_dir: str = "",
    batch_size: int = 8, force: bool = False, ref_cache: dict = None,
) -> tuple:
    """渲染一个人设的台词列表。返回 (done, skipped, failed)。

    ``ref_cache``：本轮跨人设复用缓存 {(ref_fp, key): ogg_path}——多个人设共享
    同一参考音（同一音色）时，同一台词只烧一次 GPU，其余人设直接**复制**成品
    （当前 7 人设仅 2 个音色，省 ~70% 夜间 GPU 时间）。
    """
    import shutil as _sh

    from src.ai.avatar_voice import find_reference_text, load_reference_b64, to_voice_note
    from src.ai.voice_prerender import (
        PRERENDER_DIRNAME,
        normalize_prerender_text,
        prerender_key,
        read_ref_manifest,
        ref_content_fp,
        write_prerendered,
        write_ref_manifest,
    )

    out_dir = Path(base_dir) / persona / PRERENDER_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    ref_text = ref_text or find_reference_text(ref)
    ref_b64 = load_reference_b64(ref)
    fp = ref_fingerprint(ref)
    cache = ref_cache if ref_cache is not None else {}

    # 备货生命周期：目录登记的参考音指纹与当前不一致（人设换声了）→ 整目录
    # 重渲（等效 --force），否则旧音色 clips 会因「文件已存在」被 skip 永不更新。
    # 无登记（legacy/新目录）不强制——渲染完成后会补登记。
    manifest = read_ref_manifest(persona, base_dir=base_dir)
    if (manifest and manifest.get("ref_sha1")
            and manifest.get("ref_sha1") != ref_content_fp(ref)):
        print(f"[*] [{persona}] 参考音已更换（指纹漂移）→ 整目录重渲")
        force = True

    # 归一化去重（渲染与在线查询同一键函数——保证命中）
    norm_lines: list = []
    seen_keys: set = set()
    for text in lines:
        t = normalize_prerender_text(text)
        k = prerender_key(t)
        if t and k and k not in seen_keys:
            seen_keys.add(k)
            norm_lines.append(t)

    done = skipped = failed = 0
    for i in range(0, len(norm_lines), max(1, batch_size)):
        batch = norm_lines[i:i + max(1, batch_size)]
        todo = []
        for text in batch:
            key = prerender_key(text)
            if not force and (out_dir / f"{key}.ogg").is_file():
                skipped += 1
                if (fp, key) not in cache:
                    cache[(fp, key)] = out_dir / f"{key}.ogg"
                continue
            # 同音色复用：本轮其他人设已渲染过同台词 → 复制成品，零 GPU
            cached = cache.get((fp, key))
            if cached is not None and Path(cached).is_file():
                _sh.copyfile(cached, out_dir / f"{key}.ogg")
                (out_dir / f"{key}.txt").write_text(text, encoding="utf-8")
                print(f"    ↻ {key}.ogg（复用同音色成品）← {text[:30]}")
                done += 1
                continue
            todo.append(text)
        if not todo:
            continue
        print(f"[*] [{persona}] 批量合成 {len(todo)} 条（RTF≈2.8，请耐心）…")
        # 批级自愈重试：7858 在显存紧张时可能崩溃重启（12G 卡与 7852 同驻），
        # 请求级 0.5s 重试无意义 → 失败后 ensure_ready（计划任务拉起+轮询就绪，
        # 冷载模型可要数分钟）再整批重试一次。
        wavs = None
        for attempt in (1, 2):
            try:
                wavs = client.batch_clone(
                    todo, reference_audio_b64=ref_b64,
                    reference_text=ref_text, language=language)
                break
            except Exception as exc:
                print(f"[!] 批量合成失败(第{attempt}次): {exc}")
                if attempt == 1:
                    print("[*] 等待 7858 自愈（计划任务拉起 + 模型冷载）…")
                    if not client.ensure_ready(wait_sec=300.0, service="7858"):
                        print("[!] 7858 未能恢复就绪，跳过本批")
                        break
        if wavs is None:
            failed += len(todo)
            continue
        for text, wav in zip(todo, wavs):
            try:
                ogg, dur = to_voice_note(wav, out_dir=str(out_dir))
                final = write_prerendered(persona, text, Path(ogg), base_dir=base_dir)
                cache[(fp, prerender_key(text))] = final
                print(f"    ✓ {final.name} ({dur}s) ← {text[:30]}")
                done += 1
            except Exception as exc:
                print(f"    ✗ {text[:30]}: {exc}")
                failed += 1
    # 登记本轮备货对应的参考音指纹（全部失败则不登记，下轮再试）
    if failed == 0 or done > 0:
        write_ref_manifest(persona, ref, base_dir=base_dir)
    return done, skipped, failed


def main() -> int:
    ap = argparse.ArgumentParser(description="AvatarHub Qwen3-TTS 批量预渲染")
    ap.add_argument("--persona", default="", help="人设 id（决定参考音与输出目录）")
    ap.add_argument("--all-personas", action="store_true",
                    help="遍历所有 avatar_clone 人设，按 config/prerender_lines/ 台词库渲染"
                         "（_common.txt 共用 + <persona>.txt 专属；夜间计划任务用）")
    ap.add_argument("--lines", nargs="*", default=[], help="台词（可多条）")
    ap.add_argument("--lines-file", default="", help="台词文件（每行一条，# 开头忽略）")
    ap.add_argument("--lines-dir", default="", help="台词库目录（默认 config/prerender_lines）")
    ap.add_argument("--ref", default="", help="参考音 WAV 路径（默认从配置解析）")
    ap.add_argument("--ref-text", default="", help="参考音逐字稿（默认读参考音旁 .txt）")
    ap.add_argument("--language", default="zh")
    ap.add_argument("--base-dir", default="", help="预渲染根目录（默认 assets/voices）")
    ap.add_argument("--batch-size", type=int, default=8, help="单请求台词条数上限")
    ap.add_argument("--force", action="store_true",
                    help="已存在也重渲（人设换参考音后旧音色过期时用）")
    args = ap.parse_args()

    if not args.all_personas and not args.persona:
        print("[!] 需要 --persona <id> 或 --all-personas")
        return 2

    cfg = _load_config()
    from src.ai.avatar_voice import AvatarVoiceClient
    from src.ai.voice_prerender import (
        DEFAULT_BASE_DIR,
        DEFAULT_LINES_DIR,
        read_prerender_lines,
    )

    client = AvatarVoiceClient.from_config(cfg)
    base_dir = args.base_dir or str(_ROOT / DEFAULT_BASE_DIR)
    lines_dir = args.lines_dir or str(_ROOT / DEFAULT_LINES_DIR)

    # 目标集：--all-personas 自动收集；否则单人设（台词=CLI 指定 ∪ 台词库）
    if args.all_personas:
        targets = _collect_avatar_personas(cfg)
        if not targets:
            print("[!] 没有 avatar_clone 人设可渲染（检查 profiles_runtime voice_profile）")
            return 2
        plan = []
        for pid, ref in targets:
            lines = read_prerender_lines(pid, lines_dir=lines_dir)
            if lines:
                plan.append((pid, ref, lines))
            else:
                print(f"[-] [{pid}] 无台词（{lines_dir} 下无 _common.txt/{pid}.txt），跳过")
    else:
        lines = [x.strip() for x in args.lines if x.strip()]
        if args.lines_file:
            for raw in Path(args.lines_file).read_text(encoding="utf-8").splitlines():
                s = raw.strip()
                if s and not s.startswith("#"):
                    lines.append(s)
        if not lines:
            lines = read_prerender_lines(args.persona, lines_dir=lines_dir)
        if not lines:
            print("[!] 没有台词可渲染（--lines / --lines-file / 台词库均空）")
            return 2
        ref = args.ref or _resolve_ref(args.persona, cfg)
        if not ref or not Path(ref).is_file():
            print(f"[!] 参考音不存在: {ref!r}（--ref 显式指定或先给人设配 voice_profile）")
            return 2
        plan = [(args.persona, ref, lines)]

    if not plan:
        print("[!] 计划为空，无事可做")
        return 0

    # 就绪等待给足 600s：7858 懒加载冷载实测可达 3-4 分钟（显存紧张时更久），
    # 300s 曾在首跑时踩超时。夜间无人等待，宁可多等。
    print(f"[*] 检查 Qwen3-TTS(7858) 就绪…")
    if not client.ensure_ready(wait_sec=600.0, service="7858"):
        print("[!] 7858 未就绪（计划任务拉起失败或超时），退出")
        return 1

    t0 = time.monotonic()
    total_done = total_skip = total_fail = 0
    ref_cache: dict = {}   # 跨人设同音色复用（同台词只烧一次 GPU）
    for pid, ref, lines in plan:
        print(f"[*] ── {pid}：{len(lines)} 条台词 ──")
        d, s, f = render_persona(
            client, pid, ref, lines, ref_text=args.ref_text,
            language=args.language, base_dir=base_dir,
            batch_size=args.batch_size, force=args.force, ref_cache=ref_cache)
        total_done += d
        total_skip += s
        total_fail += f

    dt = time.monotonic() - t0
    print(f"[*] 全部完成: 成功 {total_done} / 跳过(已存在) {total_skip} "
          f"/ 失败 {total_fail}，耗时 {dt:.0f}s")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
