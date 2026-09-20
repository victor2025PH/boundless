# -*- coding: utf-8 -*-
"""逐语种克隆语音验收流水线（P2-9 2026-08-31，「开闸前先验收」的机器裁决半场）。

用途：回答「hub 引擎 X 用生产声纹档念语种 L，出来的是不是这门语言、像不像本人」
——lang_engines 开闸（生产 overlay）前的证据生产器。人耳抽检仍是最终裁决
（产物落 tmp_langverify/ 逐句可听）；本工具把机器能判的先判完：

  ① hub /api/tts_only 信封解码（绝不 -OutFile 直存；content-type 必须 JSON）
  ② magic bytes（RIFF）——尺寸/200 不构成内容验证（2026-08-13 事故纪律）
  ③ WAV 采样率指纹 vs 引擎预期（fish=44100/index=22050/moss=24000，
     防 hub prefer 语义静默顶包＝「合成成功但换了引擎」）
  ④ ASR 回转（198:8765 large-v3-turbo，language 不传＝自动检测）：
     Whisper 自检语种 == 目标语——「念出来的是不是日语」的直接判据
     （怪声/错语言在这里现形；这正是 08-31 事故的漏网面）
  ⑤ 通用字符级 CER（与 synth_verify 多语轨同一实现 _cer_chars，单一事实源）
  ⑥ hub /api/clone_score 声纹分（≥0.80 地板；接口异常记 WARN 不判死——
     声纹是加分证据，人耳兜底）

纪律：每语种第 1 句失败即停该语种（首样本验证通过前禁止批量）。
退出码：全 PASS=0；任何语种 FAIL=1（可进脚本/CI）。

用法（缺省即「zh 基线对照 + ja/es 走 fish」三组）：
  python tools/verify_clone_lang.py --profile 林小雨-智聊
  python tools/verify_clone_lang.py --profile 苏婉 --cases ja:fish_speech
  python tools/verify_clone_lang.py --profile 林小雨-智聊 --cases th:fire_red --n 1

只读性质：合成探针烧少量 hub GPU、产物只落本地 tmp_langverify/，
不发任何消息、不写任何配置。
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import struct
import sys
import time
import urllib.request
import uuid
from pathlib import Path

# 引擎根导入（工具从引擎根跑：python tools/verify_clone_lang.py）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ai.tts_pipeline import _cer_chars  # noqa: E402  单一事实源：与 synth_verify 多语轨同实现

# 引擎 → 原生采样率指纹（与 avatar_voice 内置三引擎表同源；目录 capabilities
# 可探到时以目录为准——新引擎零改码）
_RATE_HINTS = {"index_tts": 22050, "index_tts2": 22050,
               "moss_ttsd": 24000, "fish_speech": 44100}

# 每语种金标句（短、自然、含疑问+陈述；改动会改变 CER 语料——加句别改句）
_GOLDEN = {
    "zh": ["今天过得怎么样呀？晚上想吃点什么？",
           "我刚忙完，现在有空啦，你说的那件事我记得呢。",
           "周末要不要一起出去走走？天气应该不错。"],
    "ja": ["今日はどこかへ出かけましたか？",
           "ご飯はもう食べましたか？無理しないでくださいね。",
           "また一緒に遊びに行きましょうね。"],
    "es": ["¿Ya has comido algo hoy? Cuídate mucho.",
           "Me alegra mucho poder hablar contigo hoy.",
           "¿Quieres salir a pasear este fin de semana?"],
    "en": ["How was your day today? Don't work too late.",
           "I just finished my work, so I have some free time now.",
           "Shall we go out for a walk this weekend?"],
    "ko": ["오늘 하루는 어땠어요? 밥은 챙겨 먹었어요?",
           "방금 일 끝났어요, 이제 시간 있어요.",
           "주말에 같이 산책하러 갈래요?"],
    "de": ["Wie war dein Tag heute? Hast du schon etwas gegessen?",
           "Ich habe gerade Feierabend, jetzt habe ich etwas Zeit.",
           "Wollen wir am Wochenende zusammen spazieren gehen?"],
    "fr": ["Comment s'est passée ta journée? Tu as déjà mangé quelque chose?",
           "Je viens de finir le travail, j'ai un peu de temps maintenant.",
           "On va se promener ensemble ce week-end?"],
    "it": ["Com'è andata la tua giornata? Hai già mangiato qualcosa?",
           "Ho appena finito di lavorare, adesso ho un po' di tempo.",
           "Andiamo a fare una passeggiata insieme questo fine settimana?"],
    "ru": ["Как прошёл твой день? Ты уже поела что-нибудь?",
           "Я только что закончила работу, теперь есть немного времени.",
           "Пойдём вместе погулять в выходные?"],
}

_CER_FLOOR = 0.35        # 与 synth_verify.foreign_cer_threshold 同刻度
# 声纹刻度（Phase10 campplus 实测认知）：正常带 0.78~0.86；<0.70 才是灾难级
# （换错参考音/文件坏/模型退化）。0.80 那个地板的语境是 best_of 多候选互筛，
# 单发验收用它会把正常生产水位判死（实测 zh×index_tts 基线=0.793）。
_CLONE_SCORE_FAIL = 0.70   # 低于此=灾难级，FAIL
_CLONE_SCORE_WARN = 0.78   # 低于此=低于正常带，WARN（人耳重点听，不判死）


def _post_json(url: str, payload: dict, timeout: float) -> tuple[dict, str]:
    """POST JSON → (解析后的 dict, content-type)。非 JSON 信封直接抛（纪律①）。"""
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
        ctype = str(resp.headers.get("Content-Type") or "")
        body = resp.read()
    if "json" not in ctype.lower():
        raise RuntimeError(f"响应不是 JSON 信封（content-type={ctype}）——按纪律拒收")
    return json.loads(body.decode("utf-8")), ctype


def _wav_sample_rate(data: bytes) -> int:
    """读 WAV fmt 块采样率；非 RIFF/解析失败返回 0。"""
    if len(data) < 36 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return 0
    # 遍历 chunk 找 fmt（有的编码器在 fmt 前塞 LIST 等块）
    off = 12
    while off + 8 <= len(data):
        cid = data[off:off + 4]
        (size,) = struct.unpack("<I", data[off + 4:off + 8])
        if cid == b"fmt " and off + 12 <= len(data):
            (rate,) = struct.unpack("<I", data[off + 12:off + 16])
            return int(rate)
        off += 8 + size + (size % 2)
    return 0


def _wav_peak(data: bytes) -> float:
    """16-bit PCM WAV 峰值（0..1）。解析失败返回 -1（不可判）。

    静音产物必须在 ASR 之前拦：Whisper 对静音会幻觉出「ご視聴ありがとう
    ございました」类套话（本工具首跑实锤），语言/CER 判据全被幻觉污染。
    """
    try:
        import wave as _wave

        with _wave.open(io.BytesIO(data), "rb") as w:
            if w.getsampwidth() != 2:
                return -1.0
            n = w.getnframes()
            step = max(1, n // 48000)
            peak = 0
            for idx in range(0, n, step):
                w.setpos(idx)
                frame = w.readframes(1)
                if len(frame) >= 2:
                    v = abs(struct.unpack("<h", frame[:2])[0])
                    if v > peak:
                        peak = v
            return peak / 32768.0
    except Exception:
        return -1.0


def _asr_transcribe(asr_base: str, wav: bytes, timeout: float) -> tuple[str, str]:
    """198 OpenAI 兼容 ASR：multipart 上传、language 不传＝自动检测。

    返回 (转写文本, Whisper 自检语种)。verbose_json 契约见 scripts/asr176。
    """
    boundary = f"----verify{uuid.uuid4().hex[:12]}"
    buf = io.BytesIO()

    def field(name: str, value: str) -> None:
        buf.write(f"--{boundary}\r\nContent-Disposition: form-data; "
                  f"name=\"{name}\"\r\n\r\n{value}\r\n".encode())

    field("model", "large-v3-turbo")
    field("response_format", "verbose_json")
    buf.write(f"--{boundary}\r\nContent-Disposition: form-data; "
              f"name=\"file\"; filename=\"probe.wav\"\r\n"
              f"Content-Type: audio/wav\r\n\r\n".encode())
    buf.write(wav)
    buf.write(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        asr_base.rstrip("/") + "/audio/transcriptions", data=buf.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Authorization": "Bearer local"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
        data = json.loads(resp.read().decode("utf-8"))
    return (str(data.get("text") or "").strip(),
            str(data.get("language") or "").strip().lower())


def _clone_score(hub: str, profile: str, wav: bytes, timeout: float):
    """hub 声纹分（campplus 余弦）。任何异常返回 None（WARN 不判死）。"""
    try:
        data, _ = _post_json(
            hub.rstrip("/") + "/api/clone_score",
            {"profile": profile,
             "audio_base64": base64.b64encode(wav).decode("ascii")},
            timeout)
        v = data.get("score", data.get("cosine", data.get("similarity")))
        return float(v) if v is not None else None
    except Exception:
        return None


# Whisper 语种码 → 本工具目标语前缀的等价组（Whisper 偶给宏码/变体）
_LANG_EQUIV = {"zh": {"zh", "yue"}, "ja": {"ja"}, "es": {"es"},
               "en": {"en"}, "ko": {"ko"}, "th": {"th"}, "vi": {"vi"},
               "de": {"de"}, "fr": {"fr"}, "it": {"it"}, "ru": {"ru"}}


def verify_case(hub: str, asr: str, profile: str, lang: str, engine: str,
                n: int, out_dir: Path, timeout: float,
                clone_url: str = "", ref_b64: str = "",
                ref_text: str = "", lang_tag: bool = False) -> dict:
    """跑一个「语种×引擎」验收组。返回 {lang, engine, pass, rows:[...]}。

    两种合成模式：
    - hub 档模式（缺省）：``/api/tts_only``（profile 在 hub 侧注册）；
    - **直连克隆模式**（``clone_url`` 非空）：``/v1/tts/clone`` 直传本地参考音
      （fish 契约家族：117:7852 CosyVoice3 / 104:7865 IndexTTS-2）——绕开
      hub 档体系，验「引擎 × 我们自己的参考音」这条最终生产形态；此模式无
      prefer 语义顶包风险，采样率只记录不判死。
    """
    rows = []
    sents = _GOLDEN.get(lang) or []
    if not sents:
        return {"lang": lang, "engine": engine, "pass": False,
                "rows": [], "error": f"无金标句（_GOLDEN 缺 {lang}）"}
    ok_all = True
    for i, text in enumerate(sents[:max(1, n)], 1):
        row = {"i": i, "text": text}
        rows.append(row)
        try:
            t0 = time.monotonic()
            if clone_url:
                # lang_tag：CosyVoice 系跨语言控制走**文内语言标签**（与粤语线
                # clone_text_prefix "<|yue|>" 同机制；language 字段实测被 117:7852
                # 忽略——日文汉字被按中文读音念出）。tag 由 tokenizer 消费不会
                # 被读出；CER 对照仍用原文本。
                synth_text = (f"<|{lang}|>{text}" if lang_tag else text)
                payload = {"text": synth_text, "reference_audio_b64": ref_b64,
                           "language": lang, "return_base64": True}
                if ref_text:
                    payload["reference_text"] = ref_text
                data, _ = _post_json(
                    clone_url.rstrip("/") + "/v1/tts/clone", payload, timeout)
            else:
                payload = {"profile": profile, "text": text, "language": lang,
                           "best_of": 1}
                if engine:
                    payload["tts_engine"] = engine
                data, _ = _post_json(hub.rstrip("/") + "/api/tts_only",
                                     payload, timeout)
            row["synth_ms"] = int((time.monotonic() - t0) * 1000)
            if not data.get("audio_base64") or (
                    "ok" in data and data.get("ok") is not True):
                row["fail"] = f"信封 ok!=true / 无音频（{str(data)[:120]}）"
                ok_all = False
                break
            wav = base64.b64decode(data["audio_base64"])
            # ② magic bytes
            if wav[:4] != b"RIFF":
                row["fail"] = f"magic bytes 非 RIFF（前 4 字节 {wav[:4]!r}）"
                ok_all = False
                break
            # ③ 采样率指纹（引擎归属；直连模式无顶包风险→只记录不判死）
            rate = _wav_sample_rate(wav)
            row["rate"] = rate
            want = (None if clone_url
                    else _RATE_HINTS.get(str(engine or "").strip().lower()))
            if want and rate and rate != want:
                row["fail"] = (f"采样率 {rate} ≠ 引擎 {engine} 预期 {want}"
                               f"——疑似 hub prefer 语义顶包，拒收")
                ok_all = False
                break
            fp = out_dir / f"{lang}-{engine or 'default'}-{i}.wav"
            fp.write_bytes(wav)
            row["file"] = str(fp)
            # ③b 能量检测（先于 ASR：静音会诱发 Whisper 幻觉套话，污染语种/CER 判据）
            peak = _wav_peak(wav)
            row["peak"] = round(peak, 4) if peak >= 0 else None
            if 0 <= peak < 0.004:
                row["fail"] = f"疑似哑音（峰值 {peak:.4f}）——引擎对该语种输出静音"
                ok_all = False
                break
            # ④ ASR 回转 + 语种自检
            hyp, asr_lang = _asr_transcribe(asr, wav, timeout)
            row["asr_lang"] = asr_lang
            row["hyp"] = hyp
            (fp.with_suffix(".txt")).write_text(
                f"ref: {text}\nhyp: {hyp}\nasr_lang: {asr_lang}\n",
                encoding="utf-8")
            equiv = _LANG_EQUIV.get(lang, {lang})
            if asr_lang and asr_lang not in equiv:
                row["fail"] = (f"ASR 自检语种 {asr_lang} ≠ 目标 {lang}"
                               f"——念出来的不是这门语言（怪声形态）")
                ok_all = False
                break
            # ⑤ 字符级 CER
            cer = _cer_chars(hyp, text)
            row["cer"] = round(cer, 3) if cer >= 0 else None
            if cer < 0 or cer > _CER_FLOOR:
                row["fail"] = f"CER {row['cer']} 超地板 {_CER_FLOOR}"
                ok_all = False
                break
            # ⑥ 声纹分（灾难级才判死；低于正常带记 WARN 交人耳）
            score = _clone_score(hub, profile, wav, timeout)
            row["clone_score"] = score
            if score is not None and score < _CLONE_SCORE_FAIL:
                row["fail"] = (f"声纹分 {score:.3f} 低于灾难地板 "
                               f"{_CLONE_SCORE_FAIL}——疑似换错参考音/文件坏")
                ok_all = False
                break
            if score is not None and score < _CLONE_SCORE_WARN:
                row["warn"] = (f"声纹 {score:.3f} 低于正常带下缘 "
                               f"{_CLONE_SCORE_WARN}（人耳重点听）")
            row["pass"] = True
        except Exception as ex:
            row["fail"] = f"{type(ex).__name__}: {ex}"
            ok_all = False
            break   # 纪律：首样本没过，禁止批量
    return {"lang": lang, "engine": engine, "pass": ok_all, "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hub", default="http://192.168.0.176:9000")
    ap.add_argument("--asr", default="http://192.168.0.198:8765/v1",
                    help="OpenAI 兼容 ASR base（生产 voice_recognition 同源）")
    ap.add_argument("--profile", required=True,
                    help="hub 声纹档名（声纹分对照用；直连模式也用它算 clone_score）")
    ap.add_argument("--cases", default="zh:index_tts,ja:fish_speech,es:fish_speech",
                    help="逗号分隔的 语种:引擎 组；zh 组是链路基线对照")
    ap.add_argument("--clone-url", default="",
                    help="直连克隆模式：/v1/tts/clone 节点（如 http://127.0.0.1:7852）"
                         "——绕 hub 档直传 --ref 参考音；cases 的引擎段仅作标注")
    ap.add_argument("--ref", default="", help="直连模式参考音 wav 路径")
    ap.add_argument("--ref-text", default="",
                    help="参考音逐字稿（缺省自动找 --ref 旁同名 .txt）")
    ap.add_argument("--lang-tag", action="store_true",
                    help="直连模式在合成文本前拼 <|lang|> 语言标签"
                         "（CosyVoice 系跨语言控制，与粤语线同机制）")
    ap.add_argument("--n", type=int, default=3, help="每语种句数（首句过了才跑后续）")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--out", default="tmp_langverify")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args()

    ref_b64 = ""
    ref_text = args.ref_text
    if args.clone_url:
        if not args.ref or not Path(args.ref).is_file():
            print(f"--clone-url 模式需要有效 --ref 参考音（当前: {args.ref!r}）")
            return 2
        ref_b64 = base64.b64encode(Path(args.ref).read_bytes()).decode("ascii")
        if not ref_text:
            side = Path(args.ref).with_suffix(".txt")
            if side.is_file():
                ref_text = side.read_text(
                    encoding="utf-8", errors="replace").strip()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for case in [c.strip() for c in args.cases.split(",") if c.strip()]:
        lang, _, engine = case.partition(":")
        r = verify_case(args.hub, args.asr, args.profile,
                        lang.strip().lower(), engine.strip(),
                        args.n, out_dir, args.timeout,
                        clone_url=args.clone_url, ref_b64=ref_b64,
                        ref_text=ref_text, lang_tag=args.lang_tag)
        results.append(r)
        if not args.json:
            flag = "PASS" if r["pass"] else "FAIL"
            print(f"[{flag}] {r['lang']} x {r['engine'] or '(hub默认)'} "
                  f"profile={args.profile}")
            for row in r["rows"]:
                bits = [f"  #{row['i']}"]
                if "rate" in row:
                    bits.append(f"sr={row.get('rate')}")
                if row.get("asr_lang"):
                    bits.append(f"asr_lang={row['asr_lang']}")
                if row.get("cer") is not None:
                    bits.append(f"cer={row['cer']}")
                if row.get("clone_score") is not None:
                    bits.append(f"clone={row['clone_score']:.3f}")
                if row.get("synth_ms"):
                    bits.append(f"{row['synth_ms']}ms")
                if row.get("warn"):
                    bits.append(f"WARN({row['warn']})")
                bits.append("OK" if row.get("pass") else
                            ("FAIL: " + str(row.get("fail") or "?")))
                print(" ".join(bits))
            if r.get("error"):
                print("  " + r["error"])
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
    all_pass = all(r["pass"] for r in results) if results else False
    if not args.json:
        print(f"== 总判定: {'PASS' if all_pass else 'FAIL'} "
              f"（产物在 {out_dir}/ 供人耳抽检；机器判据过了≠免听）==")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
