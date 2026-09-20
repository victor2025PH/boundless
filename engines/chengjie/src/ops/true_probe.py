"""true_probe — 四域「真活探针」（2026-08-17 无兜底纪律，实施33 v2 规则 8）。

health 200 不算活：8/16-8/17 事故里 index worker /health 恒 200 但 CPU 爬行 40-120s、
fish 健康 ping 绿但真合成挂死——全天语音断档零告警。本模块对四个域周期性
**真干活**（真合成 / 真翻译 / 真识图 / 真转写），连续 N 次失败＝主机弹窗 +
EventBus(host_alert) 外发 + ERROR 日志；恢复补绿窗。

「有回话」同样不算活（2026-08-27 补）：出文本的三个域此前只断言输出非空——VLM 丢了
视觉塔、或图被服务端悄悄丢弃后照样闲聊，MT 把源文原样回吐（模型装错最常见的形态），
ASR 把 2 倍速噪声也能吐出一串字（当天实锤，见 ASR_FIXTURE），在旧判据下全是绿的。
故内容级弱断言进 spec，见 ``content_verdict``；断言词表一律拿**已知坏样本**校准。

结构：
  - ``build_probe_specs(cfg)``  纯函数：从运行配置推每域探针规格（未启用的域无规格）
  - ``probe_gap_reasons(cfg)``  纯函数：域「开着却推不出规格」＝静默盲区，逐条给原因
  - ``content_verdict(...)``    纯函数：回答的内容级弱断言（离线可单测）
  - ``run_probe(spec)``         同步 HTTP 执行一个规格（watchdog tick 在线程池里跑，可阻塞）
  - ``next_strike_state(...)``  纯函数：连败/恢复状态机 → 该发什么通知
  - watchdog 侧薄包装见 health_watchdog._check_true_probes
  - 手动跑一轮（不等 10min tick）：``python tools/true_probe_selfcheck.py``
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ai_chat_assistant.true_probe")

# ASR 探针语音夹具（hub index_tts 合成「你好你好，今天天气不错」，2026-08-17 生成；
# 2026-08-27 修正为 16k 单声道 PCM）。
# ⚠ 原始夹具是**第二个从没被验过的夹具**：WAV 头声明 44100Hz，而 index_tts 原生是
# 22050Hz（仓库 media-artifact 纪律里的采样率指纹），于是整段音频以 2 倍速+高八度
# 播放，ASR 十天来一直把它转成「挺好 挺好 挺好 挺挺挺不錯」——而探针只查「非空」，
# 所以每 10 分钟照报 asr=ok。按 22050 重解释即逐字还原「今天天氣不錯」，据此定案。
# 逐字稿 sidecar 在 assets/probe/asr_probe.txt（与参考音同约定）——「它应该念什么」
# 必须和音频放在一起，光写在代码注释里，动夹具的人看不到。
# 换夹具必须：跑 test_asr_fixture_is_canonical_16k_wav（离线结构）+
# `python tools/true_probe_selfcheck.py --domain asr -n 3`（真机复核 _ASR_EXPECT）。
# 附带好处：内容断言上线后，**夹具再被写坏会让探针直接红**——2 倍速那版转出的
# 「挺挺挺不錯」过不了 今天/天氣 断言，不会再像这次一样静默绿十天。
ASR_FIXTURE = Path(__file__).resolve().parents[2] / "assets" / "probe" / "asr_probe.wav"

# 转写断言词表：用**已知坏样本**校准——2 倍速那版输出「挺挺挺不錯」，含「不錯」却
# 不含「今天/天氣」。故 不错/不錯 刻意排除在外（它连坏样本都能满足＝没有鉴别力），
# 只收 ASR 真听清了才会出现的词。简繁两版都收（language=auto 的输出会漂）。
_ASR_EXPECT: Tuple[str, ...] = ("今天", "天气", "天氣")

# 64x64 纯红 PNG（识图探针输入；活性探测不校验内容正确性，只要求模型真跑了推理）。
# 改这串前先读两条硬约束（2026-08-27 事故沉淀，当天 vision 探针每 10min 红一次）：
#   ① 必须是**结构完好**的 PNG。旧常量是 8x8 且 IDAT chunk 的 CRC 是坏的、尾部缺
#      IEND；LAN ollama 解码宽容照收（探针长期绿），03:10 识图切硅基后严格校验直接
#      400：`{"code":20015,"message":"Verify image file failed: broken PNG file
#      (bad header checksum in b'IDAT')"}`。**夹具本身从没被验过**是本次真正的根因。
#   ② 边长必须够大。已知厂商地板是 28px（Qwen3-VL 图像处理器：重新生成的**合法**
#      8x8 一样被 400 拒——`height(8) or width(8) must be larger than 28 for Qwen 3
#      VL models`），这里取 64 是**刻意留余量**：紧贴地板 4px 的夹具，下一家把地板
#      抬高就再炸一次，而尺寸带来的 token/带宽成本可以忽略（168B）。
# 门禁 test_probe_image_fixture_is_structurally_valid 钉住这两条（纯离线校 CRC+尺寸）。
# 实测：硅基 Qwen3-VL-8B 与 LAN ollama qwen3-vl:8b-instruct 两家都答「鲜艳的红色」。
_RED_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAb0lEQVR4nO3PAQkAAAyEwO9f"
    "eoshgnABdLep8QUNyPEFDcjxBQ3I8QUNyPEFDcjxBQ3I8QUNyPEFDcjxBQ3I8QUNyPEFDcjx"
    "BQ3I8QUNyPEFDcjxBQ3I8QUNyPEFDcjxBQ3I8QUNyPEFDcjxBQ3IPanc8OLDQitxAAAAAElF"
    "TkSuQmCC"
)

_AUDIO_MAGIC = (b"RIFF", b"OggS", b"ID3", b"fLaC")

# 识图断言词表：喂纯红图问主色调，真看见了就几乎不可能不提到红。刻意**只收窄集**
# ——rot/rouge/rojo 之流是别的语言的「红」但同时是 rotate/protection 的子串，收进来
# 只会制造假绿。假绿是次要损失（漏判），假红要人半夜起床，宁可漏判不误判。
_VISION_EXPECT: Tuple[str, ...] = ("红", "red", "赤")

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def content_verdict(content: str, spec: Dict[str, Any]) -> Tuple[bool, str]:
    """openai_chat 回答的内容级弱断言（纯函数，离线可单测）。→ (ok, 失败原因)。

    两条声明式判据（都写在 spec 里，是纯数据不是回调）：
      ``expect_any``   命中任一子串（大小写不敏感）＝证明模型真消费了输入。
      ``expect_latin`` 结构式：必须有拉丁字母且不得含 CJK＝证明 zh→en 真译了，
                       而不是把源文原样回吐（词表匹配对 MT 太脆，结构判据才稳）。

    判据一律选「真跑对了就几乎不可能不满足」的那一侧——探针误红的代价（弹窗+
    信任流失，2026-08-27 实锤）远大于偶尔漏判。
    """
    text = (content or "").strip()
    if not text:
        return False, "empty completion"
    expect = spec.get("expect_any") or ()
    if expect:
        low = text.lower()
        if not any(str(k).lower() in low for k in expect):
            return False, f"answer misses {list(expect)[:3]}: {text[:48]!r}"
    if spec.get("expect_latin"):
        if not any("a" <= c <= "z" for c in text.lower()):
            return False, f"no latin in output: {text[:48]!r}"
        if _CJK_RE.search(text):
            return False, f"source echoed (CJK in output): {text[:48]!r}"
    return True, ""

# hub 侧一次 tts_only 的内部预算（换引擎/换副本重试）实测可达 100-120s，而探针
# 超时即断连、hub 那边请求仍在途 → 副本被判「疑似挂死」并被后续路由规避，
# 于是探针本身把业务链推得更坏（2026-08-21 实锤：24 次 wedge 警告全在探针放弃后）。
# 故 tts 探针超时须**大于** hub 内部预算：默认 90s，且不许配到 60s 以下。
_TTS_PROBE_TIMEOUT_DEFAULT = 90.0
_TTS_PROBE_TIMEOUT_FLOOR = 60.0


def _tts_probe_timeout(hf: Dict[str, Any]) -> float:
    """tts 探针超时（秒）：``hub_fish.probe_timeout_sec`` 可调，地板 60s。"""
    try:
        v = float(hf.get("probe_timeout_sec") or 0.0)
    except (TypeError, ValueError):
        v = 0.0
    if v <= 0:
        v = _TTS_PROBE_TIMEOUT_DEFAULT
    return max(v, _TTS_PROBE_TIMEOUT_FLOOR)


def _vision_endpoints(vi: Dict[str, Any]) -> List[str]:
    """识图端点顺序——**复用** vision_client 的解析，绝不在这里另算一套。

    生产按 ``_vision_base_urls`` 解析（``base_urls`` 在前、单数 ``base_url`` 在后，
    补 ``/v1`` 并去重）。探针此前只认单数 ``base_url``：两者当前恰好同值，一旦有人
    只改 ``base_urls``，探针就会在测**另一个端点**还报绿——「探针测的不是生产在用
    的那条路」是本模块最不该犯的错。导入失败回落单数 base_url（旧行为，零破坏）。
    """
    try:
        from src.vision_client import _vision_base_urls
        return list(_vision_base_urls(vi) or [])
    except Exception:
        one = str(vi.get("base_url") or "").strip()
        return [one] if one else []


def _vision_endpoint_model(vi: Dict[str, Any], url: str) -> str:
    """每端点模型名——同样复用 vision_client（实施71 的 ``endpoint_models``）。

    云主与 LAN 备对同一模型命名不同（``Qwen/Qwen3-VL-8B-Instruct`` vs
    ``qwen3-vl:8b-instruct``），两套解析必然漂移；漂移的后果是探针拿不存在的模型名
    打备胎、把健康端点判死。私有名导入是刻意的：复用 > 复刻。
    """
    default = str(vi.get("model") or "").strip()
    try:
        from src.vision_client import _endpoint_model
        return str(_endpoint_model(vi, url, default) or default)
    except Exception:
        return default


# 配置里推不出预算时的兜底（旧硬编码值，保持零破坏）。有配置就一律跟配置走。
_VISION_PROBE_FALLBACK_TIMEOUT = 45.0


def _vision_endpoint_timeout(vi: Dict[str, Any], url: str) -> float:
    """每端点探测预算——**跟生产同一个数**，别在探针里另立一套「多久算太久」。

    2026-08-27 老板定线：「20 秒不识图就是有问题，要反馈」。那条线一旦只写进生产
    配置而探针自留 45s 硬编码，就会出现最难查的一种分裂：**生产已经按 15s 掐断并
    报失败，探针却还在慢悠悠等到 45s 然后报绿**——看板说健康、客户在挨等，而且谁
    调了生产超时都不会想到还有第二个数要跟着改。这与本模块 ``_vision_endpoints`` /
    ``_vision_endpoint_model`` 复用 vision_client 是同一条理由：复用 > 复刻。

    用生产预算来卡探针是**偏宽松**的，方向安全：探针喂的是 64x64 小图，生产是
    1536x1536 大图（同日实测云端 6.6s 中位就是按生产参数打的）。小图都跑不进大图的
    预算，生产必然已经坏了——不会误报，只会晚报一点点。
    """
    try:
        from src.vision_client import _endpoint_timeout
        return float(_endpoint_timeout(
            vi, url, default=float(vi.get("timeout")
                                   or _VISION_PROBE_FALLBACK_TIMEOUT)))
    except Exception:
        return _VISION_PROBE_FALLBACK_TIMEOUT


def build_probe_specs(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从运行配置推四域探针规格（纯函数）。域未启用/缺配置 → 不出规格（静默跳过）。"""
    cfg = cfg if isinstance(cfg, dict) else {}
    specs: List[Dict[str, Any]] = []

    # ── tts：hub /api/tts_only 真合成（显式钉引擎，media-artifact 纪律：验 magic）──
    av = cfg.get("avatar_voice") or {}
    hf = av.get("hub_fish") if isinstance(av.get("hub_fish"), dict) else {}
    if av.get("enabled") and hf.get("enabled") and str(hf.get("base_url") or "").strip():
        pmap = hf.get("profile_map") if isinstance(hf.get("profile_map"), dict) else {}
        profile = ""
        for _v in pmap.values():
            profile = str(_v or "").strip()
            if profile:
                break
        if profile:
            body: Dict[str, Any] = {
                "profile": profile, "text": "好的呀", "best_of": 1,
                "format": "wav", "language": "zh-cn",
            }
            _eng = str(hf.get("tts_engine") or "").strip()
            if _eng:
                body["tts_engine"] = _eng
            specs.append({
                # 2026-09-16：与 minicpm 拆成 tts_hub / tts_index——此前同 domain="tts"
                # 时 strike 按域键存，第二条 ok 会把第一条连败清零（hub 合成失败被掩盖）。
                # 拆分口径对齐 vision / vision_backup*。
                "domain": "tts_hub", "kind": "hub_tts",
                "url": str(hf.get("base_url")).rstrip("/") + "/api/tts_only",
                "json": body, "timeout": _tts_probe_timeout(hf),
                # 引擎归属判据（2026-08-22）：探针**刻意要 wav**——采样率是引擎身份证，
                # 而 ogg 转码恒重采样 48k 会把它抹平。生产多走 ogg（查不出），所以这条
                # 带外探针是「hub 有没有拿别的引擎顶包」唯一的常态化检出面。
                "engine": _eng,
            })

    # ── tts_index：智聊专属 IndexTTS-2 节点真合成（minicpm_clone /v1/tts/clone）──
    # 2026-08-29 补：上面那个分支闸在 `hub_fish.enabled` 上，而智聊的语音主力已迁到
    # 自建 104:7865（老板指令「所有音色都要 IndexTTS-2」+「不用 176 角色库」）——
    # hub_fish 一关，tts 域**整域从探针消失**，恰恰是最不该没有监控的那条链：
    # `voice_consistency=strict` + `cloud_fallback=false` 下节点挂掉是**直接停发语音**
    # 而不是降级，坐席只会看到「语音发不出去」，没有任何前置告警。
    #
    # 刻意复用 hub_tts 这个 kind：两边响应契约（`{ok, audio_base64}`）与断言（magic
    # bytes + 采样率指纹反查引擎归属）逐字相同，只有 URL 与 body 不同——复制一份执行
    # 分支只会让两条判据日后漂移。参考音复用 ASR 夹具：它本就是规范中文 WAV 且**已有
    # 逐字稿 sidecar**（IndexTTS-2 走 inference_zero_shot 保音色路径的前提），零新增资产。
    mc = cfg.get("minicpm_clone") if isinstance(cfg.get("minicpm_clone"), dict) else {}
    _mc_base = str(mc.get("base_url") or "").strip()
    if mc.get("enabled") and _mc_base and not ASR_FIXTURE.is_file():
        logger.warning(
            "[true_probe] tts_index 域（minicpm_clone）缺参考音夹具，该域不参与探针：%s",
            ASR_FIXTURE)
    if mc.get("enabled") and _mc_base and ASR_FIXTURE.is_file():
        _ref_txt = ""
        _sidecar = ASR_FIXTURE.with_suffix(".txt")
        if _sidecar.is_file():
            try:
                _ref_txt = _sidecar.read_text(encoding="utf-8").strip()
            except OSError:
                _ref_txt = ""
        specs.append({
            "domain": "tts_index", "kind": "hub_tts",
            "url": _mc_base.rstrip("/") + "/v1/tts/clone",
            "json": {
                "text": "好的呀",
                "reference_audio_b64": base64.b64encode(
                    ASR_FIXTURE.read_bytes()).decode("ascii"),
                "reference_text": _ref_txt,
                "emotion": "neutral",
            },
            "timeout": _tts_probe_timeout(mc),
            # 采样率指纹：IndexTTS-2=22050Hz。节点若被换成 fish(44100)/MOSS(24000)
            # 顶包，这条会红——与 hub 侧「prefer 语义静默换引擎」防的是同一类事故。
            "engine": "index_tts",
        })

    # ── ser：语音情绪真识别（远程 GPU emotion2vec，/v1/audio/emotion）──────────
    # 2026-08-29 补：SER 是**被动触发**的（客户发语音才调），失败会静默进 120s 冷却
    # 回落本机 CPU funasr——链路不断、只是从 ~350ms 变秒级，`speech_emotion_remote_total`
    # 那个计数器要有真实流量才看得出异常，凌晨/低峰零调用时它一直是好的。主动探针是
    # 唯一能在「没人发语音」时也发现远程 SER 死掉的面。
    # 夹具复用 ASR_FIXTURE（同一份 wav 喂两个域，零新增资产）。
    se = cfg.get("speech_emotion") if isinstance(cfg.get("speech_emotion"), dict) else {}
    _ser_rem = se.get("remote") if isinstance(se.get("remote"), dict) else {}
    _ser_base = str(_ser_rem.get("base_url") or "").strip()
    if _ser_base and not ASR_FIXTURE.is_file():
        logger.warning("[true_probe] ser 域缺夹具，该域不参与探针：%s", ASR_FIXTURE)
    if _ser_base and ASR_FIXTURE.is_file():
        try:
            _ser_to = float(_ser_rem.get("timeout_sec") or 10.0)
        except (TypeError, ValueError):
            _ser_to = 10.0
        specs.append({
            "domain": "ser", "kind": "ser_emotion",
            "url": _ser_base.rstrip("/") + "/v1/audio/emotion",
            # 探针预算比客户端超时宽一档：客户端超时即回落本地（业务不受影响），
            # 而探针的职责是判「远程到底活没活」，卡在同一秒会把慢当成死。
            "timeout": _ser_to + 5.0,
            # 内容级断言：**最高分**必须够尖。emotion2vec 正常输出是尖峰分布
            # （夹具实测 0.972，第二名 0.0098）；模型未载入/权重坏时退化成接近
            # 均匀分布（9 类 ⇒ 每类 ~0.111）。刻意**不**断言具体情绪——那会随
            # 夹具语调与模型版本漂移，而「有没有做出明确判断」才是活性的真判据。
            "min_top_score": 0.3,
        })

    # ── translate：MT 引擎真翻译（OpenAI 兼容口，单端点契约）──────────────────
    eng = ((cfg.get("translation") or {}).get("engines") or {})
    mt = eng.get("ollama_mt") if isinstance(eng.get("ollama_mt"), dict) else {}
    _mt_bases = mt.get("base_urls") or ([mt.get("base_url")] if mt.get("base_url") else [])
    if _mt_bases and str(mt.get("model") or "").strip():
        _mt_json: Dict[str, Any] = {
            "model": str(mt.get("model")).strip(),
            "messages": [{
                "role": "user",
                "content": "把下面这句话翻译成英文，只输出译文：你好，今天天气不错",
            }],
            "max_tokens": 60, "temperature": 0,
        }
        # 后端专有补丁必须与生产**同源**（`ollama_mt.payload_extra`，见 OllamaMTEngine）：
        # 少带一个 `chat_template_kwargs.enable_thinking:false`，Qwen3 系后端就会把
        # content 留空、正文全塞进 reasoning ⇒ 探针「假红」而生产其实好的（反之亦然）。
        _mt_extra = mt.get("payload_extra")
        if isinstance(_mt_extra, dict):
            _mt_json.update(_mt_extra)
        specs.append({
            "domain": "translate", "kind": "openai_chat",
            "url": _mt_chat_url(str(_mt_bases[0])),
            "json": _mt_json,
            # 译文必须真是英文：MT 把中文源文原样回吐是模型装错/权重没载最常见的
            # 形态，旧的「非空即绿」判据对它完全睁眼瞎。
            "expect_latin": True,
            "timeout": 30.0,
        })

    # ── vision：VLM 真识图（喂纯红图求一句描述），**逐端点** ────────────────
    # 端点顺序与模型名一律问 vision_client（唯一事实源，见 _vision_endpoints）：
    # 此前探针只打单数 base_url，而生产按 `base_urls` 在前的顺序解析——两者一旦分叉，
    # 探针测的就不是生产主路。备胎同样要探：备胎腐烂了若只能在云端故障当天发现，
    # 那正是最坏的时机（2026-08-27 实测 LAN 备胎当时健康，属运气不属机制）。
    vi = cfg.get("vision") or {}
    _vi_eps = _vision_endpoints(vi)
    if vi.get("enabled") and _vi_eps and str(vi.get("model") or "").strip():
        # 云端主路（实施70 识图切硅基）要真 Bearer——探针裸打 401 会把健康端点
        # 判死（2026-08-27 03:59 实锤 strike）。LAN ollama 忽略 Authorization，
        # 带真 key 无害；未配/占位仍回落 "probe" 保持旧行为。
        _vi_key = str(vi.get("api_key") or "").strip()
        _vi_headers = (
            {"Authorization": f"Bearer {_vi_key}"}
            if _vi_key and _vi_key not in ("ollama", "YOUR_ZHIPU_API_KEY")
            else None
        )
        for _idx, _url in enumerate(_vi_eps):
            specs.append({
                # 主路仍叫 vision（弹窗/告警语义不变）；备胎独立域名，各自记连败。
                "domain": "vision" if _idx == 0 else f"vision_backup{_idx}",
                "kind": "openai_chat",
                **({"headers": _vi_headers} if _vi_headers else {}),
                "url": _url.rstrip("/") + "/chat/completions",
                "json": {
                    "model": _vision_endpoint_model(vi, _url),
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "用一句话说出这张图的主色调。"},
                            {"type": "image_url", "image_url": {
                                "url": "data:image/png;base64," + _RED_PNG_B64}},
                        ],
                    }],
                    "max_tokens": 40, "temperature": 0,
                },
                # 答案必须提到红：否则「模型在回话」与「模型真看见了图」分不开——
                # 服务端悄悄丢图 / 路由到纯文本模型时，旧判据一路绿灯。
                "expect_any": list(_VISION_EXPECT),
                "timeout": _vision_endpoint_timeout(vi, _url),
                # LAN Ollama 端点：探针成功后顺手核永久钉（见 ensure_ollama_pinned）。
                # ``vision.lan_keep_alive_pin: false`` 可关；云端端点不带此标记。
                **({"pin_keep_alive": True}
                   if _is_private_url(_url)
                   and vi.get("lan_keep_alive_pin", True) else {}),
                # 备胎**只观测不弹窗**：它坏了不影响当下出话（主路还在），半夜弹窗
                # 是纯噪音；但必须进状态文件/metrics，好在切主之前就看见。
                **({} if _idx == 0 else {"alert": False}),
            })

    # ── asr：真转写（OpenAI 兼容 /audio/transcriptions，multipart 夹具 wav）────
    vr = cfg.get("voice_recognition") or {}
    _vr_base = str(vr.get("base_url") or "").strip()
    if vr.get("enabled") and _vr_base and not ASR_FIXTURE.is_file():
        # 缺夹具＝这一域**静默消失**（四域探针悄悄变三域，日志里连个名字都不出现）。
        # 2026-08-27 实测根 .gitignore 的通配 *.wav 正把夹具挡在版本控制外，干净
        # clone 必然踩到——已加 probe 例外放行，这里再补一句可见的警告兜底。
        logger.warning("[true_probe] asr 域缺夹具，该域不参与探针：%s", ASR_FIXTURE)
    if vr.get("enabled") and _vr_base and ASR_FIXTURE.is_file():
        specs.append({
            "domain": "asr", "kind": "asr_transcribe",
            "url": _vr_base.rstrip("/") + "/audio/transcriptions",
            "model": str(vr.get("model") or "whisper-1").strip(),
            # 转写必须听清：ASR 是四域里最容易「有输出即算活」的一个——模型退化/
            # 喂错音频照样吐得出字，非空判据对它完全睁眼瞎（见 _ASR_EXPECT）。
            "expect_any": list(_ASR_EXPECT),
            "timeout": 30.0,
        })
        # 回落级：与上方 vision 备胎**同一哲学**，此前只有识图做了、ASR 漏了。
        # 2026-08-28 实锤这个漏洞的代价：生产 `fallback` 的唯一候选 140:7854
        # `/health` 恒 200 且 `loaded=true`，但真送音频 6 次只成功 1 次（39s，
        # 其余 240s 超时，静置 75s 后连一次都不成）——它被显存挤下 GPU 退了 CPU。
        # 探针从不打回落级 ⇒ 「纸面兜底」可以无声存在到主路真挂的那天，而那正是
        # 最没有余地的时刻。补上之后，备胎腐烂在 metrics 里当天就看得见。
        for _fi, _fb in enumerate(_asr_fallback_levels(vr), 1):
            _fspec = _asr_fallback_spec(vr, _fb, _fi)
            if _fspec:
                specs.append(_fspec)

    return specs


def _mt_chat_url(base: str) -> str:
    """MT 端点 → OpenAI 兼容 chat URL，``/v1`` **幂等**（与 OllamaMTEngine 同源）。

    2026-08-28 实锤：云优先重分配把 LAN MT 落点从 Ollama 的 ``176:11434``（不带 /v1）
    换成 vLLM 的 ``173:8001/v1``（带 /v1）后，探针这里无条件补 ``/v1`` 拼出了
    ``…:8001/v1/v1/chat/completions`` → **404 → 翻译域 100% 假红**，而生产链其实好的
    （``OllamaMTEngine._openai_chat_url`` 本来就幂等）。差 20 分钟就要发第一条误告警。

    教训与识图那条同源：**探针拼端点时必须跟生产同一套规则**，差一个 ``/v1`` 就变成
    「探的不是生产走的那条路」。此处刻意只做幂等对齐、不 import 引擎类——
    ``translation_engines`` 会拉 httpx 等重依赖，而 ``build_probe_specs`` 是被门禁高频
    调用的纯函数；规则本身只有两行，由 ``test_mt_probe_url_is_v1_idempotent`` 钉住两边一致。

    已知偏差（登记，不在本次修）：``api: native``（Ollama 原生）下生产真实打的是
    ``/api/chat``，探针仍走 ``/v1`` 兼容层——同模型同权重，「模型活没活」的判据成立，
    但严格说不是同一条路。要消除得给 native 加一个独立 kind（响应体结构不同）。
    """
    b = str(base or "").rstrip("/")
    if not b.endswith("/v1"):
        b += "/v1"
    return b + "/chat/completions"


#: ASR 回落级里**值得探**的 provider——只有走 HTTP 的远端服务才有「/health 200 却
#: 转不出文本」这种半死形态（140:7854 实测即是）。进程内 provider（sensevoice /
#: faster_whisper / whisper_local）刻意不探：探它等于在 web 进程里再加载一份模型
#: （本机显存紧张纪律），而它们的失败模式是「加载失败」，启动期就会暴露，不需要
#: 每 10 分钟一次的真活探针。
_ASR_PROBEABLE_OPENAI = ("openai", "openai_compatible", "qwen3_asr", "funasr_api")
_ASR_PROBEABLE_AVATARHUB = ("avatar_whisper", "avatarhub")


def _asr_fallback_levels(vr: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``voice_recognition.fallback`` 归一成 dict 列表。

    与 ``VoiceTranscriberFactory.create_transcriber`` 同口径：支持单 dict 或 dict 列表，
    空列表/None ⇒ 无回落级（那正是 2026-08-28 生产实例的状态：唯一落点、零兜底）。
    """
    fb = (vr or {}).get("fallback")
    if not fb:
        return []
    items = fb if isinstance(fb, list) else [fb]
    return [x for x in items if isinstance(x, dict)]


def _asr_fallback_spec(vr: Dict[str, Any], fb: Dict[str, Any],
                       idx: int) -> Optional[Dict[str, Any]]:
    """一个回落级配置 → 探针规格；不可探/配置不全 ⇒ None（不猜、不假红）。

    **端点一律要求回落级自己显式写**（不从主路继承 base_url）：继承会让探针把主路
    端点又探一遍，白烧一次推理还伪装成「备胎健康」。AvatarHub 的 ``token_file``
    同理——转录器侧有内联默认值，探针刻意不复刻它（第二处默认值早晚与第一处分叉），
    宁可不探。所以**要让备胎进探针，`fallback` 级必须写全 `base_url`（AvatarHub 还要
    `token_file`）**，这也是我方落 fallback 配置时的既定写法。
    """
    provider = str(fb.get("provider") or vr.get("provider") or "").strip().lower()
    try:
        timeout = float(fb.get("timeout") or vr.get("timeout") or 30.0)
    except (TypeError, ValueError):
        timeout = 30.0
    common: Dict[str, Any] = {
        "domain": f"asr_backup{idx}",
        "expect_any": list(_ASR_EXPECT),
        "timeout": timeout,
        # 备胎**只观测不弹窗**（与 vision_backup 同语义）：它坏了不影响当下转写，
        # 半夜弹窗是纯噪音；但必须进状态文件/metrics，好在切主之前就看见。
        "alert": False,
    }
    if provider in _ASR_PROBEABLE_AVATARHUB:
        av = fb.get("avatar") if isinstance(fb.get("avatar"), dict) else {}
        base = str(av.get("base_url") or fb.get("base_url") or "").strip()
        token_file = str(av.get("token_file") or fb.get("token_file") or "").strip()
        if not base or not token_file:
            return None
        return {**common, "kind": "asr_transcribe_avatarhub",
                "url": base.rstrip("/") + "/transcribe_b64",
                "token_file": token_file}
    if provider in _ASR_PROBEABLE_OPENAI:
        base = str(fb.get("base_url") or "").strip()
        if not base:
            return None
        return {**common, "kind": "asr_transcribe",
                "url": base.rstrip("/") + "/audio/transcriptions",
                "model": str(fb.get("model") or vr.get("model")
                             or "whisper-1").strip()}
    return None


def probe_gap_reasons(cfg: Dict[str, Any]) -> Dict[str, str]:
    """域「开着却推不出探针规格」＝静默盲区（纯函数）→ ``{域: 人话原因}``。

    为什么单独有这个函数：``build_probe_specs`` 对配置不全的域**静默跳过**，于是
    「运营把 ASR 打开了、但夹具没随仓库分发」与「运营本来就没开 ASR」在输出里
    长得一模一样——四域探针悄悄变三域，日志里连域名都不出现。2026-08-27 实测根
    ``.gitignore`` 的通配 ``*.wav`` 正在制造前者。

    只报**前者**：域没开是运营的正常选择（zhiliao_pilot 三个媒体域全关，把它报成
    问题就是误报——探针误红的代价今天已经付过一次了）。

    「拿到规格没有」这半边一律现问 ``build_probe_specs``（唯一事实源，不复刻它的
    条件判断）；本函数只额外回答「这个域是不是开着的」。
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    got = {s.get("domain") for s in build_probe_specs(cfg)}
    gaps: Dict[str, str] = {}

    av = cfg.get("avatar_voice") or {}
    hf = av.get("hub_fish") if isinstance(av.get("hub_fish"), dict) else {}
    if av.get("enabled") and hf.get("enabled") and "tts_hub" not in got:
        gaps["tts_hub"] = ("hub_fish 已启用但推不出规格（缺 base_url 或 profile_map 为空）"
                           if str(hf.get("base_url") or "").strip()
                           else "hub_fish 已启用但缺 base_url")

    # minicpm_clone（智聊专属 IndexTTS-2 节点）同样要报缺口——否则「配了却没探针」
    # 这个状态本身是静默的，而它是 strict/不回落的单点。
    mc = cfg.get("minicpm_clone") if isinstance(cfg.get("minicpm_clone"), dict) else {}
    if mc.get("enabled") and "tts_index" not in got:
        gaps["tts_index"] = (f"minicpm_clone 已启用但参考音夹具缺失：{ASR_FIXTURE}"
                             if str(mc.get("base_url") or "").strip()
                             else "minicpm_clone 已启用但缺 base_url")

    # translate 没有 enabled 开关，「启用」的真信号是**它在不在引擎链里**——
    # 光看「配置里有没有 ollama_mt 这个键」会把 config.yaml 自带的空脚手架
    # （base_url/model 皆空、order 里根本没有它）误判成盲区（zhiliao_pilot 实锤，
    # 本函数首次真机运行即自己撞上）。order 缺省时一律不判：宁可漏不可误。
    eng = ((cfg.get("translation") or {}).get("engines") or {})
    _chain: List[str] = list(eng.get("order") or []) if isinstance(eng.get("order"), list) else []
    _per_lang = eng.get("per_lang_order")
    if isinstance(_per_lang, dict):
        for _v in _per_lang.values():
            if isinstance(_v, list):
                _chain.extend(_v)
    if "ollama_mt" in {str(x).strip() for x in _chain} and "translate" not in got:
        gaps["translate"] = "ollama_mt 在引擎链里但缺 base_url(s) 或 model"

    vi = cfg.get("vision") or {}
    if vi.get("enabled") and "vision" not in got:
        gaps["vision"] = "识图已启用但缺 base_url 或 model"

    vr = cfg.get("voice_recognition") or {}
    if vr.get("enabled") and "asr" not in got:
        gaps["asr"] = (f"语音识别已启用但夹具缺失：{ASR_FIXTURE}"
                       if str(vr.get("base_url") or "").strip()
                       else "语音识别已启用但缺 base_url")

    # SER 远程配了却推不出规格＝那台 GPU 情绪识别没有任何主动探测面（失败只会静默
    # 回落本机 CPU，低峰期连计数器都看不出来）。
    se = cfg.get("speech_emotion") if isinstance(cfg.get("speech_emotion"), dict) else {}
    _rem = se.get("remote") if isinstance(se.get("remote"), dict) else {}
    if str(_rem.get("base_url") or "").strip() and "ser" not in got:
        gaps["ser"] = f"远程 SER 已配置但夹具缺失：{ASR_FIXTURE}"
    return gaps


def _http_json(url: str, payload: Dict[str, Any], timeout: float,
               headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    hdrs = {"Content-Type": "application/json; charset=utf-8",
            "Authorization": "Bearer probe"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _ollama_root(chat_url: str) -> str:
    """``http://host:11434/v1/chat/completions`` → ``http://host:11434``；非 OpenAI
    兼容路径返回空串（不是 Ollama 形态的端点就不去碰）。"""
    u = str(chat_url or "").strip()
    marker = "/v1/chat/completions"
    if not u.endswith(marker):
        return ""
    return u[: -len(marker)].rstrip("/")


def _is_private_url(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return (host in ("localhost", "127.0.0.1") or host.startswith("192.168.")
            or host.startswith("10.")
            or bool(re.match(r"^172\.(1[6-9]|2\d|3[01])\.", host)))


# 永久钉判据：Ollama 的 keep_alive=-1 会把 expires_at 写成几百年后；任何不到一天的
# 到期时间都说明钉已经丢了（主机重启 / 有人手动改过 keep_alive）。
_PIN_MIN_REMAIN_SEC = 24 * 3600.0


def ollama_pin_needed(ps_payload: Dict[str, Any], model: str,
                      now_ts: Optional[float] = None) -> bool:
    """纯函数：``/api/ps`` 返回里该模型是否**在驻留但没打永久钉**。

    未驻留（不在 ps 里）返回 False——冷载由探针请求本身触发，这里只管「钉」，
    不替代加载；解析失败一律 False（宁可不钉，也不无脑每 10 分钟发一次 generate）。
    """
    try:
        models = ps_payload.get("models") or []
    except AttributeError:
        return False
    want = str(model or "").strip()
    if not want:
        return False
    now = float(now_ts if now_ts is not None else time.time())
    for m in models:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or m.get("model") or "")
        if name != want and name.split(":")[0] != want:
            continue
        exp = str(m.get("expires_at") or "").strip()
        if not exp:
            return True
        try:
            from datetime import datetime
            # Go 的 RFC3339 带纳秒（9 位小数），Python fromisoformat 只吃到 6 位。
            core = re.sub(r"(\.\d{6})\d+", r"\1", exp).replace("Z", "+00:00")
            remain = datetime.fromisoformat(core).timestamp() - now
        except Exception:
            return False
        return remain < _PIN_MIN_REMAIN_SEC
    return False


def ensure_ollama_pinned(chat_url: str, model: str, *, timeout: float = 5.0) -> str:
    """LAN Ollama 端点探针成功后顺手核一次永久钉；丢了就补（``keep_alive=-1``）。

    背景（2026-09-10 实锤）：176 上 qwen3-vl 的永久钉在 8-28 整机崩溃重启后没补回，
    实际 keep_alive≈10 分钟，而真活探针周期 10m+漂移 ⇒ 每轮都撞上冷载 ⇒ 3s 端点超时
    ⇒ 两连败弹 host_alert，10 分钟后又「恢复」——31 小时里 6 组共 12 条纯噪音告警。
    探针既然每 10 分钟就路过一次，就让它顺手把钉核一遍：一次 GET ``/api/ps``，
    只在真丢钉时才多发一个 ``/api/generate``。只对私网端点做（云端无此契约）。
    返回一个短字串供探针 detail 拼接（"" = 无事发生）。绝不抛。
    """
    root = _ollama_root(chat_url)
    if not root or not _is_private_url(root) or not str(model or "").strip():
        return ""
    try:
        with urllib.request.urlopen(root + "/api/ps", timeout=timeout) as resp:
            ps = json.loads(resp.read().decode("utf-8", "replace"))
        if not ollama_pin_needed(ps, model):
            return ""
        _http_json(root + "/api/generate",
                   {"model": model, "keep_alive": -1}, max(timeout, 30.0))
        logger.warning("[true_probe] %s 上 %s 永久钉丢失，已重新钉住(keep_alive=-1)",
                       root, model)
        return "repinned"
    except Exception as e:
        logger.info("[true_probe] 永久钉核查跳过 %s %s: %s", root, model, str(e)[:80])
        return ""


def _chat_content(data: Dict[str, Any]) -> str:
    try:
        return str(((data.get("choices") or [{}])[0].get("message") or {})
                   .get("content") or "").strip()
    except Exception:
        return ""


def run_probe(spec: Dict[str, Any]) -> Tuple[bool, str]:
    """执行一个探针规格 → (ok, detail)。阻塞式，调用方须在线程池里跑。绝不抛。"""
    kind = str(spec.get("kind") or "")
    url = str(spec.get("url") or "")
    timeout = float(spec.get("timeout") or 30.0)
    t0 = time.time()
    try:
        if kind == "hub_tts":
            data = _http_json(url, spec.get("json") or {}, timeout)
            if not data.get("ok"):
                return False, f"hub ok=false detail={str(data.get('detail'))[:80]}"
            raw = base64.b64decode(str(data.get("audio_base64") or ""), validate=False)
            if len(raw) < 8000 or not raw.startswith(_AUDIO_MAGIC):
                return False, f"audio invalid ({len(raw)}B)"
            base = f"{time.time() - t0:.1f}s {len(raw) // 1024}KB"
            # 「合成成功」≠「目标引擎合成」：hub 是 prefer 语义，点名引擎不可用就静默
            # 换一个，信封里没有引擎字段。按采样率指纹反查——判出别的已登记引擎即红。
            try:
                from src.ai.avatar_voice import engine_attribution
                verdict, detail = engine_attribution(
                    raw, str(data.get("format") or "wav"), spec.get("engine"))
            except Exception:
                verdict, detail = "unknown", ""
            if verdict == "mismatch":
                return False, f"engine mismatch: {detail}"
            return True, f"{base} {detail}".rstrip()
        if kind == "openai_chat":
            data = _http_json(url, spec.get("json") or {}, timeout,
                              headers=spec.get("headers"))
            # 成本记账（2026-09-08）：云端识图探针每 10 分钟真打一次硅基 Qwen3-VL，
            # 金额虽小也要进账本——对账时「探针」是独立一桶，不该混进客户回复。
            try:
                from src.ai.llm_cost import provider_from_base_url, record_usage_from_response
                record_usage_from_response(
                    data, model=str((spec.get("json") or {}).get("model") or ""),
                    purpose="probe", tier="probe",
                    provider=provider_from_base_url(url) or "lan")
            except Exception:
                pass
            content = _chat_content(data)
            verdict_ok, why = content_verdict(content, spec)
            if not verdict_ok:
                return False, why
            detail = f"{time.time() - t0:.1f}s {content[:24]!r}"
            if spec.get("pin_keep_alive"):
                tag = ensure_ollama_pinned(
                    url, str((spec.get("json") or {}).get("model") or ""))
                if tag:
                    detail += f" {tag}"
            return True, detail
        if kind == "ser_emotion":
            wav = ASR_FIXTURE.read_bytes()
            boundary = f"----probe{uuid.uuid4().hex}"
            parts = (
                f"--{boundary}\r\nContent-Disposition: form-data; "
                f"name=\"file\"; filename=\"probe.wav\"\r\n"
                f"Content-Type: audio/wav\r\n\r\n"
            ).encode("utf-8") + wav + f"\r\n--{boundary}--\r\n".encode("utf-8")
            req = urllib.request.Request(
                url, data=parts,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            labels = data.get("labels") or []
            scores = data.get("scores") or []
            if not labels or len(labels) != len(scores):
                return False, (f"结构异常 labels={len(labels)} scores={len(scores)}"
                               f"（服务端契约变了或模型未载入）")
            try:
                nums = [float(s) for s in scores]
            except (TypeError, ValueError):
                return False, "scores 非数值"
            top = max(nums)
            floor = float(spec.get("min_top_score") or 0.3)
            if top < floor:
                # 均匀分布＝模型没真在判（未载入/权重坏），而 HTTP 200 + 结构完好
                # 会让「非空即绿」的判据完全睁眼瞎——与 ASR 那次「2 倍速噪声也吐字」同源。
                return False, (f"分布过平 top={top:.3f} < {floor}"
                               f"（{len(nums)} 类均分约 {1.0 / max(len(nums), 1):.3f}）")
            emo = str(labels[nums.index(top)])
            return True, f"{time.time() - t0:.1f}s {emo}@{top:.2f}"
        if kind == "asr_transcribe":
            wav = ASR_FIXTURE.read_bytes()
            boundary = f"----probe{uuid.uuid4().hex}"
            parts = (
                f"--{boundary}\r\nContent-Disposition: form-data; "
                f"name=\"model\"\r\n\r\n{spec.get('model')}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; "
                f"name=\"file\"; filename=\"probe.wav\"\r\n"
                f"Content-Type: audio/wav\r\n\r\n"
            ).encode("utf-8") + wav + f"\r\n--{boundary}--\r\n".encode("utf-8")
            req = urllib.request.Request(
                url, data=parts,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Authorization": "Bearer probe",
                },
                method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            text = str(data.get("text") or "").strip()
            verdict_ok, why = content_verdict(text, spec)
            if not verdict_ok:
                return False, why.replace("empty completion", "empty transcription")
            return True, f"{time.time() - t0:.1f}s {text[:20]!r}"
        if kind == "asr_transcribe_avatarhub":
            # AvatarHub STT（140:7854）的契约与主路 OpenAI 兼容口完全不同源：
            # POST /transcribe_b64 + X-AH-Svc 令牌 + base64 载荷。复用 avatar_voice
            # 的载荷/解析函数，与 AvatarWhisperTranscriber 走**同一套编解码**——
            # 探针手搓一份就会变成「探的和生产走的不是一条路」。
            from src.ai.avatar_voice import (
                build_stt_payload, parse_stt_response, resolve_service_token)
            token = resolve_service_token(str(spec.get("token_file") or ""))
            if not token:
                return False, "X-AH-Svc token unavailable"
            # 夹具语言**已知是中文**，故显式 "zh"：这与「用户任意语音必须走空串
            # 自动检测」（2026-07-13 实测契约，强制语种会把外语音频变成翻译）不矛盾
            # ——那条规则约束的是真实入站音频，这里是固定夹具，钉死语种判据最稳。
            payload = build_stt_payload(ASR_FIXTURE.read_bytes(), language="zh")
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json", "X-AH-Svc": token},
                method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            text = str(parse_stt_response(body) or "").strip()
            verdict_ok, why = content_verdict(text, spec)
            if not verdict_ok:
                return False, why.replace("empty completion", "empty transcription")
            return True, f"{time.time() - t0:.1f}s {text[:20]!r}"
        return False, f"unknown kind {kind!r}"
    except urllib.error.HTTPError as ex:
        # 响应体才是根因。裸 str(HTTPError) 只有「HTTP Error 400: Bad Request」，
        # 零信息量——2026-08-27 那次坏 PNG 事故靠它排查不出任何东西，得手工复现
        # 请求才看到 `code 20015 broken PNG file`。4xx 尤其必须带上厂商的话。
        try:
            body = " ".join(ex.read().decode("utf-8", "replace").split())
        except Exception:
            body = ""
        return False, f"HTTP {ex.code}: {(body or str(ex.reason))[:180]}"
    except Exception as ex:
        return False, f"{type(ex).__name__}: {str(ex)[:100]}"


STATE_FILENAME = "true_probe_state.json"

# 持久化的连败状态最多认这么久。进程停了几天再回来，旧的 alerted 不该继续压住一次
# 合法首报——把「防弹窗风暴」做成「永久静音」是更坏的失败模式。
STATE_MAX_AGE_SEC = 24 * 3600.0


def state_path(config_dir: Any) -> Path:
    """状态文件位置（与 identity_shadow_state.json 同目录同风格）。"""
    return Path(config_dir) / STATE_FILENAME


def read_state(path: Any) -> Dict[str, Any]:
    """读状态（缺失/坏 JSON/形状不对 → 空 dict，绝不抛）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_state(
    path: Any,
    strikes: Dict[str, Dict[str, Any]],
    *,
    observations: Optional[Dict[str, Dict[str, Any]]] = None,
    gaps: Optional[Dict[str, str]] = None,
    now: Optional[float] = None,
) -> bool:
    """原子落盘（tmp+replace）。失败只记 debug——丢的是去抖基准与观测，绝不能反过来
    影响探针本身（探针是兜底机制，兜底机制不该新增故障面）。"""
    ts = float(now if now is not None else time.time())
    doc: Dict[str, Any] = {
        "updated_ts": ts,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
        "domains": {},
        "gaps": dict(gaps or {}),
    }
    for domain, st in (strikes or {}).items():
        row = dict(st or {})
        row.update((observations or {}).get(domain) or {})
        doc["domains"][domain] = row
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        return True
    except Exception:
        logger.debug("[true_probe] 状态落盘失败 path=%s", path, exc_info=True)
        return False


def restore_strike_state(
    path: Any,
    *,
    now: Optional[float] = None,
    max_age_sec: float = STATE_MAX_AGE_SEC,
) -> Dict[str, Dict[str, Any]]:
    """冷启动恢复连败状态（修「同一个问题每次重启弹一次窗」）。

    2026-08-27 实录：识图探针红了一整天，04:48/05:28/06:18 三次重启把内存里的
    ``alerted`` 清零，于是同一个问题弹了三次窗。持久化后：问题没解决就不再重复报，
    真恢复了才补绿窗。

    只认新鲜状态（默认 24h）：太旧的一律丢弃当全新开始——宁可多报一次，也不要让
    一个陈年状态文件把新故障的首报永久静音。形状不对/读不到 → {}。
    """
    doc = read_state(path)
    ts = float(now if now is not None else time.time())
    try:
        age = ts - float(doc.get("updated_ts") or 0.0)
    except (TypeError, ValueError):
        return {}
    if age < 0 or age > float(max_age_sec):
        return {}
    domains = doc.get("domains")
    if not isinstance(domains, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for domain, row in domains.items():
        if not isinstance(row, dict):
            continue
        try:
            out[str(domain)] = {
                "fails": int(row.get("fails") or 0),
                "alerted": bool(row.get("alerted")),
                "last_ok": float(row.get("last_ok") or 0.0),
                "last_run": float(row.get("last_run") or 0.0),
                "first_fail": float(row.get("first_fail") or 0.0),
                "oks": int(row.get("oks") or 0),
            }
        except (TypeError, ValueError):
            continue
    return out


def metrics_snapshot(config_dir: Any, *, now: Optional[float] = None) -> Dict[str, Any]:
    """``/api/workspace/metrics`` 的 ``true_probe`` 段——**只读状态文件**。

    绝不在 web 请求里现场跑探针（那会把一次看板刷新变成四发真推理，且请求超时）。
    ``stale_sec`` 让消费方能区分「四域都绿」与「探针根本没在跑」——后者此前完全
    不可观测（探针结果只进日志和弹窗，src/web 里搜不到一处引用）。
    """
    doc = read_state(state_path(config_dir))
    if not doc:
        return {"present": False}
    ts = float(now if now is not None else time.time())
    domains = doc.get("domains") if isinstance(doc.get("domains"), dict) else {}
    out: Dict[str, Any] = {
        "present": True,
        "updated_at": str(doc.get("updated_at") or ""),
        "stale_sec": max(0, int(ts - float(doc.get("updated_ts") or 0.0))),
        "gaps": doc.get("gaps") if isinstance(doc.get("gaps"), dict) else {},
        "domains": {},
        "failing": [],
    }
    for domain, row in domains.items():
        if not isinstance(row, dict):
            continue
        ok = bool(row.get("ok"))
        out["domains"][str(domain)] = {
            "ok": ok,
            "fails": int(row.get("fails") or 0),
            "alerted": bool(row.get("alerted")),
            "detail": str(row.get("detail") or "")[:120],
        }
        if not ok:
            out["failing"].append(str(domain))
    return out


def stalled_verdict(
    config_dir: Any,
    *,
    interval_min: float = 10.0,
    factor: float = 3.0,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """探针**自己停摆**的判据（纯函数）→ 停摆详情，或 None（正常）。

    2026-08-27 亲身实锤：接线漏了一个局部 import → NameError 被 tick 外层的 except
    吞掉 → 探针从重启起整段不跑。当时唯一的信号是「轮次完成那行不再出现」——一个
    需要人主动去数的负面信号。兜底机制静默停摆是最坏的失败模式，它必须自己有兜底。

    **零误报的构造**：只在「状态文件存在 **且** 陈旧」时成立。
      · 文件存在 ＝ 探针至少成功跑完过一轮（这台机器上它是work 的）；
      · 陈旧     ＝ 它停了。
    从没跑过（无文件）刻意不报——那是「未启用 / 刚部署 / 配置推不出规格」，由
    metrics 的 ``present=false`` 与 ``probe_gap_reasons`` 表达，在这里报就是误报。
    """
    p = state_path(config_dir)
    doc = read_state(p)
    if not doc:
        return None
    ts = float(now if now is not None else time.time())
    try:
        updated = float(doc.get("updated_ts") or 0.0)
    except (TypeError, ValueError):
        return None
    if updated <= 0:
        return None
    threshold = max(120.0, float(interval_min or 10.0) * 60.0 * max(1.5, float(factor)))
    stale = ts - updated
    if stale <= threshold:
        return None
    return {
        "stale_sec": int(stale),
        "threshold_sec": int(threshold),
        "updated_at": str(doc.get("updated_at") or ""),
        "state_file": str(p),
    }


def next_strike_state(
    state: Dict[str, Dict[str, Any]],
    domain: str,
    ok: bool,
    *,
    fail_strikes: int = 2,
    now: Optional[float] = None,
    min_fail_sec: float = 0.0,
    recover_oks: int = 1,
) -> Tuple[Dict[str, Dict[str, Any]], str]:
    """连败状态机（纯函数）。返回 (新 state, action)。

    action ∈ ""（无事）| "alert"（达连败阈值，首报）| "recovered"（曾报过警后恢复）。
    重复失败不重复出 action（弹窗去抖由 notify_host 冷却兜第二层）。

    滞回（2026-09-10 运维群降噪 P0.4）：09-09～09-10 视觉探针 6 组「失败→10 分钟后恢复」
    成对刷屏——176 模型冷加载超 3s 超时、下一轮就好，形态是抖动不是故障。两道闸：
      - ``min_fail_sec``：连败达阈值还不够，失败还要**持续**这么久（从首败 ``first_fail``
        起算）才首报——默认 0（旧行为）；watchdog 默认给 15min，10 分钟一轮的抖动吸掉；
      - ``recover_oks``：报过警后要**连续** N 次成功才补绿窗——默认 1（旧行为）；watchdog
        默认 2，防「好一轮又坏」把恢复/再失败对着刷。
    """
    ts = float(now if now is not None else time.time())
    st = dict(state or {})
    d = dict(st.get(domain) or {"fails": 0, "alerted": False, "last_ok": 0.0})
    action = ""
    if ok:
        d["oks"] = int(d.get("oks", 0)) + 1
        if d.get("alerted"):
            if d["oks"] >= max(1, int(recover_oks)):
                action = "recovered"
                d.update({"fails": 0, "alerted": False, "first_fail": 0.0})
            # 未凑够连续成功：保持 alerted，fails 归零但不清 first_fail（再失败续算）
            else:
                d["fails"] = 0
        else:
            d.update({"fails": 0, "first_fail": 0.0})
        d["last_ok"] = ts
    else:
        d["oks"] = 0
        d["fails"] = int(d.get("fails", 0)) + 1
        if not float(d.get("first_fail") or 0.0):
            d["first_fail"] = ts
        lasted = ts - float(d.get("first_fail") or ts)
        if (d["fails"] >= max(1, int(fail_strikes))
                and lasted >= max(0.0, float(min_fail_sec))
                and not d.get("alerted")):
            d["alerted"] = True
            action = "alert"
    d["last_run"] = ts
    st[domain] = d
    return st, action


__all__ = [
    "ASR_FIXTURE",
    "STATE_FILENAME",
    "build_probe_specs",
    "content_verdict",
    "metrics_snapshot",
    "next_strike_state",
    "probe_gap_reasons",
    "read_state",
    "restore_strike_state",
    "run_probe",
    "stalled_verdict",
    "state_path",
    "write_state",
]
