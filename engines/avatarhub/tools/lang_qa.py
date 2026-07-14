# -*- coding: utf-8 -*-
"""P6-1 弱语种清单数据化(可反复重跑的常驻评测工具)。

做法：对全部候选语种,每语 2 句 → Fish 克隆合成(带参考音,与真实链路同路径)
      → 双 ASR 回转写(Nemotron 流式主力 + .140 Whisper 分段兜底) → 与原文归一化比对。
判定：
  tts_ok      = max(两引擎均值) ≥ 0.50   TTS 确实会说这门语言(可懂度代理)
  stream_weak = whisper ≥ 0.55 且 (nemo ≤ whisper-0.25 或 nemo < 0.40)
                → 流式引擎该语种明显弱于分段引擎,语向含它时应回退分段模式
产物：
  data/lang_qa.json     全量评测数据(逐语逐引擎相似度,复盘/趋势用)
  data/weak_langs.json  弱语种策略文件(live_interpreter 启动读取;Nemotron 升版后重跑本
                        工具,达标语种自动摘除,无需改代码/环境变量)
用法：
  python tools\lang_qa.py                 全量 24 语(约 3 分钟)
  python tools\lang_qa.py ko ja ru        只测指定语种(升版后复测)
"""
import sys, os, io, json, time, base64, wave, difflib, re, subprocess

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FISH = os.environ.get("FISH_URL", "http://127.0.0.1:7855")
NEMO = os.environ.get("NEMO_URL", "http://127.0.0.1:7857")
WHISPER = os.environ.get("STT140_URL", "http://192.168.0.140:7854")
REF_WAV = os.path.join(BASE, "logs", "_qa_ref.wav")

import requests

# 每语 2 句：问候 + 业务句(降低单句偶然性)。原文即评分基准。
SAMPLES = {
    "zh": ["你好，很高兴认识你。", "我们下周确认发货时间。"],
    "en": ["Hello, nice to meet you.", "Let us confirm the shipping time next week."],
    "ja": ["こんにちは、はじめまして。", "来週の出荷時間を確認しましょう。"],
    "ko": ["안녕하세요, 만나서 반갑습니다.", "다음 주 배송 시간을 확인합시다."],
    "ru": ["Здравствуйте, приятно познакомиться.", "Давайте подтвердим время отправки на следующей неделе."],
    "fr": ["Bonjour, enchanté de vous rencontrer.", "Confirmons l'heure d'expédition la semaine prochaine."],
    "de": ["Hallo, schön Sie kennenzulernen.", "Lassen Sie uns den Liefertermin für nächste Woche bestätigen."],
    "es": ["Hola, encantado de conocerte.", "Confirmemos la hora de envío la próxima semana."],
    "pt": ["Olá, prazer em conhecê-lo.", "Vamos confirmar o horário de envio na próxima semana."],
    "it": ["Ciao, piacere di conoscerti.", "Confermiamo l'orario di spedizione la prossima settimana."],
    "vi": ["Xin chào, rất vui được gặp bạn.", "Chúng ta hãy xác nhận thời gian giao hàng vào tuần tới."],
    "th": ["สวัสดีครับ ยินดีที่ได้รู้จัก", "เรามายืนยันเวลาจัดส่งในสัปดาห์หน้ากันเถอะ"],
    "id": ["Halo, senang bertemu dengan Anda.", "Mari kita konfirmasi waktu pengiriman minggu depan."],
    "ms": ["Helo, gembira berjumpa dengan anda.", "Mari kita sahkan masa penghantaran minggu depan."],
    "ar": ["مرحبا، سعيد بلقائك.", "دعنا نؤكد موعد الشحن الأسبوع المقبل."],
    "hi": ["नमस्ते, आपसे मिलकर खुशी हुई।", "आइए अगले सप्ताह शिपमेंट का समय तय करें।"],
    "tr": ["Merhaba, tanıştığımıza memnun oldum.", "Gelecek haftaki sevkiyat zamanını doğrulayalım."],
    "nl": ["Hallo, leuk je te ontmoeten.", "Laten we de verzendtijd voor volgende week bevestigen."],
    "pl": ["Cześć, miło cię poznać.", "Potwierdźmy termin wysyłki w przyszłym tygodniu."],
    "uk": ["Привіт, приємно познайомитися.", "Давайте підтвердимо час відправлення наступного тижня."],
    "tl": ["Kumusta, ikinagagalak kitang makilala.", "Kumpirmahin natin ang oras ng pagpapadala sa susunod na linggo."],
    "km": ["សួស្តី រីករាយដែលបានជួបអ្នក។", "តោះបញ្ជាក់ពេលវេលាដឹកជញ្ជូននៅសប្តាហ៍ក្រោយ។"],
    "my": ["မင်္ဂလာပါ၊ တွေ့ရတာဝမ်းသာပါတယ်။", "နောက်အပတ် ပို့ဆောင်ချိန်ကို အတည်ပြုကြရအောင်။"],
    "fa": ["سلام، از آشنایی با شما خوشوقتم.", "بیایید زمان ارسال هفته آینده را تأیید کنیم."],
}
REF_TEXT = ("This is a reference voice sample for quality testing. "
            "I am very happy to talk with you today about our new project.")


def _norm(s: str) -> str:
    return re.sub(r"[\s\u200b、。，．.!?！？:：;；'\"“”«»…\-–—,()（）]+", "", (s or "").lower())


def _ensure_ref() -> str:
    """参考音缺失时用 SAPI 现造一段(与 P5-2 同参考,保证跨次可比)。返回 b64。"""
    if not os.path.exists(REF_WAV):
        ps = ("Add-Type -AssemblyName System.Speech; "
              "$sp=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
              "$en=$sp.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'en*' } | Select-Object -First 1; "
              "if($en){$sp.SelectVoice($en.VoiceInfo.Name)}; "
              f"$out=Join-Path $env:TEMP '_qa_ref.wav'; $sp.SetOutputToWaveFile($out); "
              f"$sp.Speak('{REF_TEXT}'); $sp.Dispose(); "
              f"Copy-Item $out '{REF_WAV}' -Force")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=60)
    return base64.b64encode(open(REF_WAV, "rb").read()).decode()


def _synth(text: str, lang: str, ref_b64: str) -> str:
    r = requests.post(f"{FISH}/v1/tts/clone",
                      json={"text": text, "language": lang, "return_base64": True,
                            "temperature": 0.7, "top_p": 0.7, "repetition_penalty": 1.2,
                            "seed": 42, "reference_audio_b64": ref_b64,
                            "reference_text": REF_TEXT}, timeout=180)
    r.raise_for_status()
    return r.json().get("audio_base64", "")


def _asr(url: str, b64: str, lang: str) -> str:
    j = requests.post(f"{url}/transcribe_b64",
                      json={"audio_base64": b64, "language": lang}, timeout=120).json()
    return j.get("text", "")


def run(langs):
    ref_b64 = _ensure_ref()
    rows, weak, tts_bad = [], [], []
    for lang in langs:
        sims = {"nemo": [], "whisper": []}
        errs = []
        for text in SAMPLES[lang]:
            try:
                b64 = _synth(text, lang, ref_b64)
            except Exception as e:
                errs.append(f"tts:{str(e)[:50]}")
                continue
            for name, url in (("nemo", NEMO), ("whisper", WHISPER)):
                try:
                    hyp = _asr(url, b64, lang)
                    sims[name].append(difflib.SequenceMatcher(None, _norm(text), _norm(hyp)).ratio())
                except Exception as e:
                    sims[name].append(0.0)
                    errs.append(f"{name}:{str(e)[:40]}")
        nemo = round(sum(sims["nemo"]) / len(sims["nemo"]), 3) if sims["nemo"] else 0.0
        whis = round(sum(sims["whisper"]) / len(sims["whisper"]), 3) if sims["whisper"] else 0.0
        best = max(nemo, whis)
        tts_ok = best >= 0.50
        stream_weak = bool(whis >= 0.55 and (nemo <= whis - 0.25 or nemo < 0.40))
        if stream_weak:
            weak.append(lang)
        if not tts_ok:
            tts_bad.append(lang)
        rows.append({"lang": lang, "nemo": nemo, "whisper": whis, "best": best,
                     "tts_ok": tts_ok, "stream_weak": stream_weak,
                     "errs": errs[:4]})
        print(f"{lang}: nemo={nemo:.2f} whisper={whis:.2f} "
              f"{'STREAM-WEAK' if stream_weak else ''}{'' if tts_ok else ' TTS-LOW'}", flush=True)
    return rows, weak, tts_bad


if __name__ == "__main__":
    pick = [a.lower() for a in sys.argv[1:] if a.lower() in SAMPLES] or list(SAMPLES)
    t0 = time.time()
    rows, weak, tts_bad = run(pick)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    os.makedirs(os.path.join(BASE, "data"), exist_ok=True)
    qa_path = os.path.join(BASE, "data", "lang_qa.json")
    # 部分复测时合并进旧全量数据(只覆盖本次测的语种)
    old = {}
    try:
        old = {r["lang"]: r for r in json.load(open(qa_path, encoding="utf-8")).get("rows", [])}
    except Exception:
        pass
    for r in rows:
        old[r["lang"]] = r
    merged = sorted(old.values(), key=lambda r: r["lang"])
    json.dump({"at": stamp, "rows": merged}, open(qa_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    all_weak = sorted(r["lang"] for r in merged if r.get("stream_weak"))
    all_ttslow = sorted(r["lang"] for r in merged if not r.get("tts_ok"))
    json.dump({"weak": all_weak, "tts_low": all_ttslow, "measured_at": stamp,
               "note": "由 tools/lang_qa.py 双 ASR 实测生成;live_interpreter 启动读取。"
                       "weak=流式引擎该语种弱→语向含它自动回退分段模式;"
                       "tts_low=克隆TTS该语种发音不可懂→切到该语向时提醒改用字幕沟通。"
                       "升级引擎后重跑本工具即自动更新。"},
              open(os.path.join(BASE, "data", "weak_langs.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n{len(rows)} 语评完({time.time()-t0:.0f}s) · 流式弱语种: {all_weak or '无'}"
          f" · TTS 不达标: {sorted(tts_bad) or '无'}")
    print(f"已写 data/lang_qa.json + data/weak_langs.json")
