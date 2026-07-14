# -*- coding: utf-8 -*-
"""Telegram 原生来电 PoC 验证台（P0 闸门脚本）。

**唯一目的**：用**测试号**验证原生来电链路的三个立项闸门，产出可信实测数据：
  ①「自动接听」——能否捕获 INCOMING_CALL 并 accept；
  ②「出向音频」——能否把我方 PCM 通过 send_frame 送达对端（对端听得到）；
  ③「进向音频」——能否收到对端 PCM 帧（ntgcalls #44 历史顽疾，2.x 需绕过 pybind11，
     3.0 已把 record + stream_frame(INCOMING) 做成一等 API，本脚本实测是否真的出帧）。

⚠️ 安全铁律（本脚本强制）：
  - **只认环境变量里的测试号 session**，拒绝读取 sessions/ 下的生产 session 文件；
  - 需显式 `TG_CALL_POC_CONFIRM=i-understand-test-only` 才运行（防误对生产号动手）；
  - 不接入任何生产模块（skill_manager / 编排器），是完全独立的一次性验证台。

用法（在**隔离 worktree** + 测试号上）：
  set TG_API_ID=...           # 测试号的 api_id
  set TG_API_HASH=...
  set TG_CALL_POC_SESSION=... # 测试号 pyrogram session string（绝不用生产号）
  set TG_CALL_POC_CONFIRM=i-understand-test-only
  python -m tools.tg_call_poc

然后用**另一台手机/账号**给测试号拨 Telegram 语音电话，观察日志：
  [poc] INCOMING_CALL from ...        ← 闸门①通过
  [poc] answered, conndir=...          ← accept 成功
  [poc] OUT sent N frames              ← 闸门②：对端应能听到 440Hz 正弦音
  [poc] IN  recv N frames  bytes=...   ← 闸门③：#44 是否已修的判定证据（有帧=修好了）
  [poc] SUMMARY ...                     ← 挂断后打印总账，写 logs/tg_call_poc.jsonl

判定：三行都出现且 IN frames 持续增长 → 原生路线立项通过（brain=cascade 可上）。
若 IN frames 恒 0 → #44 仍在，转 tg2sip 传输层备选（见 docs/TG_NATIVE_CALL_POC.md）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import struct
import time
from pathlib import Path
from typing import Any, Dict, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("tg_call_poc")

# 通话帧参数：ntgcalls AudioSink 实测 10ms/帧（发 20ms 会 jitter underrun）。
_SR = 48000
_FRAME_MS = 10
_CH = 1
_FRAME_SAMPLES = _SR * _FRAME_MS // 1000        # 480
_FRAME_BYTES = _FRAME_SAMPLES * _CH * 2         # 960


def _sine_frame(phase: float, freq: float = 440.0) -> tuple[bytes, float]:
    """生成一帧 440Hz 正弦 PCM16（出向音频探针：对端应听到清晰单音）。"""
    buf = bytearray()
    step = 2 * math.pi * freq / _SR
    for _ in range(_FRAME_SAMPLES):
        val = int(0.35 * 32767 * math.sin(phase))
        buf += struct.pack("<h", val)
        phase += step
    return bytes(buf), phase


def _guard_or_exit() -> Dict[str, str]:
    """安全闸：确认测试意图 + 只认 env 测试号凭证，否则拒绝运行。

    ``TG_CALL_POC_ROLE``：``answer``（默认，待命接听，验证进向 #44）| ``call``（主动拨打
    ``TG_CALL_POC_PEER`` 指定的对端 user_id + 送出向正弦音，验证出向）。两台终端各跑一个
    角色（**两个空闲测试号**）即可全自动验证双向音频，无需人工拨号。
    """
    if os.environ.get("TG_CALL_POC_CONFIRM") != "i-understand-test-only":
        raise SystemExit(
            "拒绝运行：须 set TG_CALL_POC_CONFIRM=i-understand-test-only（本脚本仅限测试号）")
    api_id = os.environ.get("TG_API_ID", "").strip()
    api_hash = os.environ.get("TG_API_HASH", "").strip()
    session = os.environ.get("TG_CALL_POC_SESSION", "").strip()
    if not (api_id and api_hash and session):
        raise SystemExit(
            "缺少测试号凭证：需 TG_API_ID / TG_API_HASH / TG_CALL_POC_SESSION 环境变量")
    role = (os.environ.get("TG_CALL_POC_ROLE") or "answer").strip().lower()
    if role not in ("answer", "call"):
        role = "answer"
    peer = os.environ.get("TG_CALL_POC_PEER", "").strip()
    if role == "call" and not peer:
        raise SystemExit("call 角色须指定 TG_CALL_POC_PEER=对端 user_id（被拨测试号）")
    return {"api_id": api_id, "api_hash": api_hash, "session": session,
            "role": role, "peer": peer}


async def _run() -> None:
    creds = _guard_or_exit()
    try:
        from pyrogram import Client
        from pytgcalls import PyTgCalls, filters as call_filters
        from pytgcalls.types import (
            CallConfig, ChatUpdate, Device, Direction, ExternalMedia,
            MediaStream, StreamFrames, Update,
        )
    except Exception as ex:  # noqa: BLE001
        raise SystemExit(f"依赖缺失（pip install py-tgcalls==3.0.0.dev2 ntgcalls）: {ex}")

    app = Client("tg_call_poc", api_id=int(creds["api_id"]), api_hash=creds["api_hash"],
                 session_string=creds["session"], in_memory=True)
    calls = PyTgCalls(app)

    stats: Dict[str, Any] = {"in_frames": 0, "in_bytes": 0, "out_frames": 0,
                             "answered": False, "t_ring": 0.0, "t_answer": 0.0,
                             "t_first_in": 0.0, "chat_id": 0}
    active = {"chat_id": 0, "phase": 0.0, "stop": False}

    @calls.on_update(call_filters.chat_update(ChatUpdate.Status.INCOMING_CALL))
    async def _on_incoming(_c: Any, update: Update) -> None:  # noqa: ANN401
        chat_id = int(getattr(update, "chat_id", 0) or 0)
        stats["t_ring"] = time.time()
        stats["chat_id"] = chat_id
        logger.info("[poc] INCOMING_CALL from chat_id=%s  ← 闸门① 捕获来电", chat_id)
        try:
            # 拟人：真人不会 0 秒接。PoC 里象征性等 2s（生产走 core.ring_delay_sec）。
            await asyncio.sleep(2.0)
            # 出向：ExternalMedia.AUDIO → 我们用 send_frame 手动喂 PCM。
            await calls.play(chat_id, MediaStream(ExternalMedia.AUDIO),
                             config=CallConfig(timeout=30))
            # 进向：record + stream_frame(INCOMING) 处理器（#44 验证）。
            await calls.record(chat_id, MediaStream(ExternalMedia.AUDIO))
            stats["answered"] = True
            stats["t_answer"] = time.time()
            active["chat_id"] = chat_id
            logger.info("[poc] answered chat_id=%s  ← accept 成功，开始双向音频", chat_id)
            asyncio.ensure_future(_pump_out())
        except Exception as ex:  # noqa: BLE001
            logger.exception("[poc] 接听失败: %s", ex)

    @calls.on_update(call_filters.stream_frame(directions=Direction.INCOMING,
                                               devices=Device.MICROPHONE))
    async def _on_in_frames(_c: Any, update: Any) -> None:  # noqa: ANN401
        if not isinstance(update, StreamFrames):
            return
        n = len(update.frames or [])
        nb = sum(len(getattr(f, "frame", b"") or b"") for f in (update.frames or []))
        if stats["in_frames"] == 0 and n:
            stats["t_first_in"] = time.time()
            logger.info("[poc] IN  首帧到达  ← 闸门③ #44 已修（进向音频可用！）")
        stats["in_frames"] += n
        stats["in_bytes"] += nb
        if stats["in_frames"] % 200 < n:      # 约每 2s 打一次
            logger.info("[poc] IN  recv frames=%s bytes=%s", stats["in_frames"], stats["in_bytes"])

    @calls.on_update(call_filters.chat_update(ChatUpdate.Status.DISCARDED_CALL))
    async def _on_hangup(_c: Any, update: Update) -> None:  # noqa: ANN401
        active["stop"] = True
        _summary(stats)

    async def _pump_out() -> None:
        """按 10ms 墙钟节奏把 440Hz 正弦帧喂给 send_frame（出向音频探针）。"""
        cid = active["chat_id"]
        t0 = time.time()
        i = 0
        while not active["stop"]:
            try:
                frame, active["phase"] = _sine_frame(active["phase"])
                await calls.send_frame(cid, Device.MICROPHONE, frame)
                stats["out_frames"] += 1
                i += 1
                if i % 200 == 0:
                    logger.info("[poc] OUT sent frames=%s  ← 闸门② 对端应听到 440Hz 单音",
                                stats["out_frames"])
            except Exception as ex:  # noqa: BLE001
                logger.debug("[poc] send_frame 停止: %s", ex)
                break
            # 墙钟锚定，不漂移
            i_due = t0 + i * (_FRAME_MS / 1000.0)
            await asyncio.sleep(max(0.0, i_due - time.time()))

    await app.start()
    await calls.start()
    me = await app.get_me()
    role = creds["role"]
    logger.info("[poc] 已上线：@%s (id=%s) role=%s",
                getattr(me, "username", "?"), getattr(me, "id", "?"), role)
    logger.info("[poc] 帧参数 sr=%s frame_ms=%s bytes/frame=%s", _SR, _FRAME_MS, _FRAME_BYTES)

    if role == "call":
        # 主动拨打对端 user_id（P2P call）+ 送出向正弦音；同时 record 进向验证 #44。
        peer = int(creds["peer"]) if creds["peer"].lstrip("-").isdigit() else creds["peer"]
        logger.info("[poc] 主动拨打 peer=%s …", peer)
        try:
            await calls.play(peer, MediaStream(ExternalMedia.AUDIO),
                             config=CallConfig(timeout=40))
            await calls.record(peer, MediaStream(ExternalMedia.AUDIO))
            stats["chat_id"] = int(peer) if isinstance(peer, int) else 0
            stats["answered"] = True
            stats["t_answer"] = time.time()
            active["chat_id"] = stats["chat_id"]
            asyncio.ensure_future(_pump_out())
            logger.info("[poc] 呼叫已发起，送出向音频中；等对端接听后观察 IN 帧")
        except Exception as ex:  # noqa: BLE001
            logger.exception("[poc] 主动拨打失败: %s", ex)
    else:
        logger.info("[poc] answer 角色待命：另一测试号 role=call 拨本号即自动接听验证")
    try:
        await asyncio.Event().wait()      # 常驻直到 Ctrl+C
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        _summary(stats)


def _summary(stats: Dict[str, Any]) -> None:
    ring2ans = (stats["t_answer"] - stats["t_ring"]) if stats["t_answer"] else 0.0
    verdict = {
        "gate1_incoming": bool(stats["t_ring"]),
        "gate2_outbound": stats["out_frames"] > 0,
        "gate3_inbound": stats["in_frames"] > 0,     # #44 判定
    }
    line = {
        "ts": round(time.time(), 3),
        "answered": stats["answered"],
        "ring_to_answer_sec": round(ring2ans, 2),
        "in_frames": stats["in_frames"],
        "in_bytes": stats["in_bytes"],
        "out_frames": stats["out_frames"],
        "verdict": verdict,
        "pass": all(verdict.values()),
    }
    logger.info("[poc] SUMMARY %s", json.dumps(line, ensure_ascii=False))
    try:
        out = Path("logs/tg_call_poc.jsonl")
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(_run())
    except SystemExit as ex:
        logger.error("%s", ex)
