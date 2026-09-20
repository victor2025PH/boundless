# -*- coding: utf-8 -*-
"""专属歌 worker（实施58 P2）：pending 订单 → LLM 填词 → 声库锚 remix 渲染 →
成品落盘 → review（人审在 /singing 点唱台，放行=就地投递）。

分工边界：运行时（skill_manager intake）只建单；本 worker 是唯一的
LLM+GPU 消费者（独立进程，引擎零阻塞）；投递在引擎 approve 路由。

GPU 开工闸（impl60 交接：176 同时扛分离引擎+fish 兜底+网关，饱和会拖垮
聊天语音回落路）：网关 vram ratio ≥ 0.92 → 本轮整批让路 exit 3。

用法：
  python -m scripts.song_custom_worker                    # 消化 ≤5 张 pending
  python -m scripts.song_custom_worker --max-orders 2
  python -m scripts.song_custom_worker --demo --demo-persona chen_meiling \
      --demo-name 阿泽 --demo-facts "我上周去了海边|最近在学吉他"
失败语义：单张失败记 failed 继续批；LLM/渲染基建缺席 exit 2；全成 exit 0。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts import song_factory as sf  # noqa: E402
from scripts._data_root import load_merged_config, resolve_data_roots  # noqa: E402
from src.companion.song_lyric_writer import write_custom_lyrics  # noqa: E402
from src.companion.song_orders import SongOrderStore  # noqa: E402
from src.companion.song_stock import resolve_singing_cfg, templates_dir  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

log = sf.log
CUSTOM_AUDIO_SUBDIR = Path("assets") / "custom_songs"
VRAM_RATIO_CEILING = 0.92


def build_llm_chat(cfg: Dict[str, Any]):
    """数据根合并配置的 ai 主链 → 单轮 chat 回调；缺 key 回 None。"""
    ai = (cfg or {}).get("ai") or {}
    base = str(ai.get("base_url") or "").rstrip("/")
    key = str(ai.get("api_key") or "")
    model = str(ai.get("model") or "")
    if not base or not key or "YOUR_API" in key or not model:
        return None

    def _chat(prompt: str) -> str:
        req = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps({
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                # deepseek-v4-flash 是推理模型：reasoning_content 先吃预算，
                # max_tokens 给小了 content 直接空（首跑实锤 220 → 三稿全空）
                "temperature": 0.9, "max_tokens": 2000,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode("utf-8"))
        return str(((d.get("choices") or [{}])[0].get("message")
                    or {}).get("content") or "")

    return _chat


def gpu_gate_ok() -> bool:
    """176 让路闸（impl60 交接：分离引擎/fish 兜底/网关同宿主）。

    v2 语义修正（首跑实锤）：裸 vram ratio 夜里恒 ≥0.9——那是闲置 ollama/可泊
    引擎的**可回收**占用，渲染管线自带 clear_for_ace 会腾。真正的「别添乱」
    信号＝网关 streaming/busy（直播/对话进行中绝不碰卡）；ratio 只作观测记录。
    """
    try:
        r = sf._req(sf.GATEWAY, "/api/gpu/status", timeout=15)
        ratio = float(((r or {}).get("vram") or {}).get("ratio") or 0)
        streaming = bool((r or {}).get("streaming"))
        busy = bool((r or {}).get("busy"))
        if streaming or busy:
            log(f"直播/对话进行中（streaming={streaming} busy={busy} "
                f"ratio={ratio:.2f}），本轮让路")
            return False
        log(f"GPU 闸放行（ratio={ratio:.2f}，闲置占用交渲染管线自清）")
        return True
    except Exception as e:  # noqa: BLE001
        log(f"GPU 状态探测失败（{e}），保守让路")
        return False


def voice_for_persona(voices: List[Dict[str, Any]], persona_id: str
                      ) -> Optional[Dict[str, Any]]:
    for v in voices:
        if persona_id in (v.get("personas") or []):
            return v
    return None


def process_order(order: Dict[str, Any], store: SongOrderStore,
                  *, root: Path, tdir: Path,
                  voices: List[Dict[str, Any]], llm_chat, tries: int) -> bool:
    oid = int(order["id"])
    pid = str(order.get("persona_id") or "")
    voice = voice_for_persona(voices, pid)
    if voice is None:
        store.set_failed(oid, f"voice_missing:{pid}")
        log(f"  [{oid}] 人设 {pid} 无声库映射 → failed")
        return False
    facts: List[str] = []
    try:
        facts = [str(x) for x in json.loads(order.get("facts_json") or "[]")]
    except Exception:
        pass
    log(f"  [{oid}] 填词（素材 {len(facts)} 条，人设 {pid}，嗓 {voice['key']}）")
    lyrics, meta = write_custom_lyrics(
        llm_chat, persona_name=pid, peer_name=str(order.get("peer_name") or ""),
        facts=facts, tries=3)
    if not lyrics:
        store.set_failed(oid, f"lyrics_rejected:{str(meta)[:120]}")
        log(f"  [{oid}] 填词三稿全废 → failed")
        return False
    log("    词：" + " / ".join(lyrics.splitlines()))
    take = sf.render_voice_song(voice, lyrics, tdir, tries)
    if take is None:
        store.set_failed(oid, "render_failed")
        log(f"  [{oid}] 渲染 {tries} 抽全败 → failed")
        return False
    ogg = sf.ffmpeg_ogg(take["final"])
    body, ext = (ogg, ".ogg") if ogg else (take["final"], ".wav")
    adir = root / CUSTOM_AUDIO_SUBDIR
    adir.mkdir(parents=True, exist_ok=True)
    audio_path = adir / f"order_{oid}{ext}"
    audio_path.write_bytes(body)
    vm = sf.voice_match(pid, take["final"])   # vs 本人说话声（点唱台三档灯）
    ok = store.set_review(
        oid, lyrics=lyrics,
        take={"seed": take["seed"], "sim": take["sim"], "hits": take["hits"],
              "dur": take["dur"], "attempt": take["attempt"],
              "voice": voice["key"], "voice_match": vm,
              "lyric_meta": meta.get("ok_attempt")},
        audio_path=str(audio_path))
    log(f"  [{oid}] {'→ review（待人审）' if ok else '状态迁移失败'} "
        f"sim={take['sim']} voice_match={vm} dur={take['dur']}s")
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="专属歌 worker")
    ap.add_argument("--max-orders", type=int, default=5)
    ap.add_argument("--tries", type=int, default=4)
    ap.add_argument("--data-root", default="")
    ap.add_argument("--demo", action="store_true",
                    help="造一张演示单（chat_key=me，仅试听不投递）")
    ap.add_argument("--demo-persona", default="chen_meiling")
    ap.add_argument("--demo-name", default="")
    ap.add_argument("--demo-facts", default="",
                    help="演示素材，| 分隔")
    args = ap.parse_args(argv)

    root = resolve_data_roots(args.data_root)[0]
    log(f"数据根 {root}")
    cfg = load_merged_config(root)
    scfg = resolve_singing_cfg(cfg)
    tdir = templates_dir(scfg, root=root)
    voices = sf.load_voices(tdir)
    if not voices:
        log("voices.json 空/缺失，无法渲染")
        return 2
    llm_chat = build_llm_chat(cfg)
    if llm_chat is None:
        log("ai 主链凭证缺失，无法填词")
        return 2
    store = SongOrderStore(root=root)

    if args.demo:
        facts = [x.strip() for x in (args.demo_facts or "").split("|")
                 if x.strip()]
        oid = store.create_order(
            platform="telegram", account_id="demo", chat_key="me",
            persona_id=args.demo_persona, peer_name=args.demo_name,
            request_text="（演示单）给我写首歌", facts=facts, daily_cap=0)
        log(f"演示单已建 id={oid}" if oid else "演示单未建（同会话活单在场）")

    if not gpu_gate_ok():
        return 3

    done = fail = 0
    for _ in range(max(1, args.max_orders)):
        order = store.claim_next_pending()
        if order is None:
            break
        if process_order(order, store, root=root, tdir=tdir,
                         voices=voices, llm_chat=llm_chat, tries=args.tries):
            done += 1
        else:
            fail += 1
    log(f"本轮 done={done} fail={fail} 剩余 pending="
        f"{store.counts().get('pending', 0)}")
    print("WORKER DONE" if fail == 0 else f"WORKER PARTIAL fail={fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
