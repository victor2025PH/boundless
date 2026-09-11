# -*- coding: utf-8 -*-
"""OpenAI SDK 客户端构造「快败」ratchet 门禁（2026-08-01）。

同一类事故三处实锤：OpenAI SDK 构造时**标量 timeout 会把 connect 超时一并抬高**
（httpx 语义：标量作用于所有阶段），叠加 SDK 默认 ``max_retries=2``，打到宕机的
LAN 主机 = 3 次 TCP 连接尝试 × Windows ~21s ≈ **65 秒黑洞**：

  - 2026-07 主聊天客户端修过（断云出话从分钟级降 ~10s）；
  - 2026-07 兜底/池客户端同批修过；
  - 2026-08-01 嵌入客户端漏了 → 176 宕机时坐席拟稿凭空多等 65s（gen=68269 实测）。

本 ratchet 按构造点清单钉住：**热路 LAN 客户端**必须 ``max_retries=0`` 且附近有
``connect=`` 有界超时；**旁路/云端** 站点进台账带理由（标量超时有界或 SDK 默认
connect=5 已兜底）。新增构造点不入表即红——第四次遗漏在入库前就被拦下。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple

_SRC = Path(__file__).resolve().parents[1] / "src"

# 热路 LAN 客户端：文件 → 期望构造点数。每个构造点必须满足：
#   ① 调用文本含 max_retries=0（关 SDK 内建重试，端点轮询由调用方自己做）；
#   ② 构造点前 25 行内出现 connect=（httpx.Timeout 拆分连接/读超时）。
_REQUIRED_FASTFAIL: Dict[str, int] = {
    "ai/ai_client.py": 5,             # 主链 / 嵌入双活 / 本地兜底 / 云 Key 池 / 模型路由档
    #                                   （_build_route_clients，2026-09-02 加；connect=5 + max_retries=0）
    # ollama_mt 已于 2026-08-09 弃用 AsyncOpenAI（/v1 兼容层忽略请求级 keep_alive
    # → 模型反复冷加载），改走 httpx 原生 /api/chat，连接 5s 快败语义在
    # OllamaMTEngine._post_chat 内保留（httpx.Timeout(read, connect=5.0)）。
    "vision_client.py": 1,            # VLM 双活（176/140）——早已合规，钉住防回退
}

# 台账：允许不带快败参数的构造点（文件 → (数量, 理由)）。理由必须回答
# 「为什么慢失败在这里可接受」——新站点想进这张表需要同样的论证。
_ACCEPTED_SLOW: Dict[str, Tuple[int, str]] = {
    "ai/tts_pipeline.py": (1, "云 TTS 后端（openai backend）：无 timeout 实参 → SDK 默认 "
                              "connect=5s 已有界；云端点自有重试价值"),
    "ai/audio_pipeline.py": (1, "云 ASR 后端：同上，SDK 默认 connect=5s 有界"),
    "eval/embedding_providers.py": (1, "离线评测 CLI：不在服务热路，慢失败只拖慢评测本身"),
    "companion/deep_persona_runtime.py": (
        2, "embedder（LAN、回复路）已快败（connect=5 + max_retries=0，2026-08-01）；"
           "llm 精修打云端且 off 热路——connect 已有界、**刻意保留** SDK 重试"
           "（云端瞬时抖动重试有价值，不阻塞坐席），故整档留在台账而非 REQUIRED"),
    "voice_transcriber.py": (
        1, "参数化构造：max_retries 默认 0、timeout 默认 30s 标量（单次尝试、"
           "connect ≤ min(30, OS~21)s 有界）；转录链自有 176→140→本机 CPU 多级回落"),
    "web/routes/assistant_routes.py": (
        1, "小智 docless 直连流式（2026-08-27）：max_retries=0 已关 SDK 重试；未拆 connect= → "
           "SDK 默认 connect=5s 有界；端点由 assistant.query.llm 配（inherit 主链，云端为主），"
           "流式自带 stream_options/前端超时预算，不在坐席出话热路"),
    "ai/companion_selfie.py": (
        1, "云端出图后端（gpt-image-1/dall-e）：生成本身 10-60s 量级、异步媒体链"
           "自带预算与回落，SDK 默认重试对云端点有价值"),
}

_CALL_RE = re.compile(r"\b(?:AsyncOpenAI|OpenAI)\s*\(")


def _scan_sites() -> Dict[str, List[Tuple[int, str, str]]]:
    """src/ 下全部 OpenAI SDK 客户端构造点 → {relpath: [(lineno, call_text, ctx_before)]}。

    call_text = 从构造名起到括号配平为止；ctx_before = 前 25 行（找 connect= 证据）。
    跳过 import 行与注释行。
    """
    out: Dict[str, List[Tuple[int, str, str]]] = {}
    for fp in sorted(_SRC.rglob("*.py")):
        text = fp.read_text(encoding="utf-8", errors="replace")
        lines = text.split("\n")
        rel = fp.relative_to(_SRC).as_posix()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("from ") \
                    or stripped.startswith("import "):
                continue
            m = _CALL_RE.search(line)
            if not m:
                continue
            # 括号配平提取调用文本（跨行）
            buf, depth, started = [], 0, False
            for j in range(i, min(i + 30, len(lines))):
                seg = lines[j] if j > i else lines[j][m.start():]
                for ch in seg:
                    if ch == "(":
                        depth += 1
                        started = True
                    elif ch == ")":
                        depth -= 1
                buf.append(seg)
                if started and depth <= 0:
                    break
            call_text = "\n".join(buf)
            ctx_before = "\n".join(lines[max(0, i - 25):i])
            out.setdefault(rel, []).append((i + 1, call_text, ctx_before))
    return out


def test_all_construction_sites_classified_and_compliant():
    sites = _scan_sites()
    problems: List[str] = []

    for rel, occurrences in sorted(sites.items()):
        if rel in _REQUIRED_FASTFAIL:
            want = _REQUIRED_FASTFAIL[rel]
            if len(occurrences) != want:
                problems.append(
                    f"{rel}: 构造点数 {len(occurrences)} ≠ 登记 {want}——新增/删除了"
                    "客户端构造，先按快败纪律处理再更新登记数")
            for lineno, call_text, ctx in occurrences:
                if "max_retries=0" not in call_text.replace(" ", ""):
                    problems.append(
                        f"{rel}:{lineno}: 缺 max_retries=0（SDK 默认 2 重试 × 死主机"
                        "连接超时 = 一次调用 ~65s 黑洞）")
                if "connect=" not in call_text and "connect=" not in ctx:
                    problems.append(
                        f"{rel}:{lineno}: 构造点附近无 connect= 有界超时——标量 "
                        "timeout 会把 connect 一并抬高，宕机主机吃满整读超时")
        elif rel in _ACCEPTED_SLOW:
            want, _reason = _ACCEPTED_SLOW[rel]
            if len(occurrences) != want:
                problems.append(
                    f"{rel}: 构造点数 {len(occurrences)} ≠ 台账 {want}——新增站点须"
                    "自证「为什么慢失败可接受」或按快败纪律构造")
        else:
            for lineno, _c, _x in occurrences:
                problems.append(
                    f"{rel}:{lineno}: 未登记的 OpenAI 客户端构造点——热路 LAN 端点进 "
                    "_REQUIRED_FASTFAIL（max_retries=0 + connect= 短超时），云端/旁路"
                    "进 _ACCEPTED_SLOW 并写明理由")

    assert not problems, "\n" + "\n".join(problems)


def test_ledgers_not_stale():
    """防表过期：登记的文件必须真的存在构造点（删光了要清表，别留死条目）。"""
    sites = _scan_sites()
    for rel in list(_REQUIRED_FASTFAIL) + list(_ACCEPTED_SLOW):
        assert rel in sites, f"{rel} 已无 OpenAI 客户端构造点——从登记表移除"
