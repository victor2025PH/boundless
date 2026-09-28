/**
 * chatx-voice 冒烟：WAV 解析 / 转 MP3 / 朗读文本清洗 / 配额闸 / 何时回语音 / TTS·ASR 中继合同（mock fetch）。
 * 运行：npx tsx lib/chatx-voice.test.ts
 * env 必须在动态 import 前设好——ai-gateway 在 import 时读中继地址。
 */
import assert from "assert";
import fs from "fs";
import os from "os";
import path from "path";

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "chatx-voice-"));
const REF = path.join(TMP, "ref.mp3");
fs.writeFileSync(REF, Buffer.from("ID3fake-reference-audio"));
process.env.CHATX_BOT_VOICE_REF = REF;
process.env.TTS_RELAY_URLS = "http://tts-a.test,http://tts-b.test";
process.env.ASR_RELAY_URLS = "http://asr.test/v1";
process.env.AH_SERVICE_TOKEN = "svc-token";
process.env.CHATX_VOICE_COOLDOWN_MS = "1000";
process.env.CHATX_VOICE_USER_DAILY_MAX = "3";
process.env.CHATX_VOICE_DAILY_MAX = "5";

/** 16-bit PCM WAV：1 kHz 正弦，可选声道数。 */
function makeWav(sampleRate: number, channels: number, seconds: number): Buffer {
  const frames = Math.round(sampleRate * seconds);
  const data = Buffer.alloc(frames * channels * 2);
  for (let i = 0; i < frames; i++) {
    const s = Math.round(Math.sin((2 * Math.PI * 1000 * i) / sampleRate) * 12000);
    for (let c = 0; c < channels; c++) data.writeInt16LE(c === 0 ? s : -s, (i * channels + c) * 2);
  }
  const h = Buffer.alloc(44);
  h.write("RIFF", 0, "ascii");
  h.writeUInt32LE(36 + data.length, 4);
  h.write("WAVE", 8, "ascii");
  h.write("fmt ", 12, "ascii");
  h.writeUInt32LE(16, 16);
  h.writeUInt16LE(1, 20);
  h.writeUInt16LE(channels, 22);
  h.writeUInt32LE(sampleRate, 24);
  h.writeUInt32LE(sampleRate * channels * 2, 28);
  h.writeUInt16LE(channels * 2, 32);
  h.writeUInt16LE(16, 34);
  h.write("data", 36, "ascii");
  h.writeUInt32LE(data.length, 40);
  return Buffer.concat([h, data]);
}

type Hit = { url: string; headers: Record<string, string>; body: string | ArrayBuffer | null };
const hits: Hit[] = [];
let ttsStatus = 200;
let ttsPayload: Record<string, unknown> | null = null;
let asrStatus = 200;
let asrPayload: Record<string, unknown> = { text: "  能接 WhatsApp 吗 " };

globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
  const url = String(input);
  const headers = Object.fromEntries(new Headers(init?.headers as HeadersInit).entries());
  const body = init?.body instanceof ArrayBuffer ? init.body : typeof init?.body === "string" ? init.body : null;
  hits.push({ url, headers, body });
  if (url.includes("tts-")) {
    return new Response(JSON.stringify(ttsPayload ?? {}), { status: ttsStatus, headers: { "Content-Type": "application/json" } });
  }
  if (url.startsWith("http://asr.test/")) {
    return new Response(JSON.stringify(asrPayload), { status: asrStatus, headers: { "Content-Type": "application/json" } });
  }
  throw new Error(`unexpected fetch ${url}`);
}) as typeof fetch;

async function main() {
  const V = await import("./chatx-voice");
  assert.ok(V.voiceOutEnabled() && V.voiceInEnabled(), "配了中继就启用");

  // WAV 解析 + 转 MP3
  const mono = makeWav(24000, 1, 0.5);
  const pw = V.parseWav(mono)!;
  assert.strictEqual(pw.sampleRate, 24000);
  assert.strictEqual(pw.channels, 1);
  assert.strictEqual(pw.pcm.length, 12000);
  const m1 = (await V.wavToMp3(mono))!;
  assert.ok(m1.mp3.length > 500, "MP3 有内容");
  assert.ok(m1.mp3[0] === 0xff && (m1.mp3[1] & 0xe0) === 0xe0, "MP3 帧同步头");
  assert.ok(Math.abs(m1.durationSec - 0.5) < 0.01, "时长 0.5s");
  const stereo = makeWav(22050, 2, 0.3);
  const m2 = (await V.wavToMp3(stereo))!;
  assert.ok(Math.abs(m2.durationSec - 0.3) < 0.01, "双声道混成单声道后时长不变");
  assert.strictEqual(m2.sampleRate, 22050);
  assert.strictEqual(V.parseWav(Buffer.from("not a wav at all")), null, "非 WAV → null");
  const f32 = makeWav(16000, 1, 0.1);
  f32.writeUInt16LE(3, 20); // format=IEEE float
  assert.strictEqual(await V.wavToMp3(f32), null, "非 16-bit PCM → null（上层放弃语音）");
  assert.strictEqual(await V.wavToMp3(makeWav(16000, 1, 0)), null, "空数据 → null");

  // 朗读文本清洗
  assert.strictEqual(V.ttsText("👉 下载安装：https://bd2026.cc/download/chatx?src=a&tg=1 ，**免费**试用 (Windows)"), "下载安装： ，免费试用 Windows");
  assert.strictEqual(V.ttsText("第一句。\n\n  第二句 🎉  "), "第一句。\n第二句");

  // 何时回语音：显式 > 人设 > 是否语音进
  assert.strictEqual(V.wantsVoiceReply({}, false), false, "默认纯文字用户不附语音");
  assert.strictEqual(V.wantsVoiceReply({}, true), true, "用户发语音 → 回语音");
  assert.strictEqual(V.wantsVoiceReply({ persona: "lover" }, false), true, "恋爱陪聊默认回语音");
  assert.strictEqual(V.wantsVoiceReply({ voice: false, persona: "lover" }, true), false, "显式关 > 一切");
  assert.strictEqual(V.wantsVoiceReply({ voice: true }, false), true, "显式开");

  // 配额闸：冷却 1s / 单人 3 次 / 全局 5 次（env）
  V.resetVoiceQuota();
  const t0 = Date.parse("2026-09-23T02:00:00Z");
  assert.strictEqual(V.takeVoiceSlot(1, 0, t0), "empty");
  assert.strictEqual(V.takeVoiceSlot(1, V.VOICE_TEXT_MAX + 1, t0), "too_long");
  assert.strictEqual(V.takeVoiceSlot(1, 20, t0), null, "第一次通过");
  assert.strictEqual(V.takeVoiceSlot(1, 20, t0 + 500), "cooldown");
  assert.strictEqual(V.takeVoiceSlot(1, 20, t0 + 1000), null);
  assert.strictEqual(V.takeVoiceSlot(1, 20, t0 + 2000), null);
  assert.strictEqual(V.takeVoiceSlot(1, 20, t0 + 3000), "user_quota", "单人第 4 次拒");
  assert.strictEqual(V.takeVoiceSlot(2, 20, t0 + 3000), null);
  assert.strictEqual(V.takeVoiceSlot(3, 20, t0 + 3000), null);
  assert.strictEqual(V.takeVoiceSlot(4, 20, t0 + 3000), "global_quota", "全局第 6 次拒");
  assert.strictEqual(V.takeVoiceSlot(1, 20, t0 + 86_400_000), null, "跨天清零");
  V.resetVoiceQuota();

  // TTS 合同：/v1/tts/clone + 参考音 b64 + 语言/情感 + X-AH-Svc；WAV → MP3
  hits.length = 0;
  ttsPayload = { ok: true, audio_base64: makeWav(24000, 1, 0.4).toString("base64") };
  const s1 = await V.synthesizeVoice("你好，我是小界。", "zh", "lover");
  assert.ok(s1, "合成成功");
  assert.ok(Math.abs(s1!.durationSec - 0.4) < 0.01);
  assert.ok(s1!.mp3[0] === 0xff, "输出是 MP3");
  assert.strictEqual(hits.length, 1);
  assert.strictEqual(hits[0].url, "http://tts-a.test/v1/tts/clone");
  assert.strictEqual(hits[0].headers["x-ah-svc"], "svc-token", "集群令牌由网关代注");
  const req = JSON.parse(String(hits[0].body)) as Record<string, unknown>;
  assert.strictEqual(req.text, "你好，我是小界。");
  assert.strictEqual(req.reference_audio_b64, fs.readFileSync(REF).toString("base64"), "参考音 = CHATX_BOT_VOICE_REF");
  assert.strictEqual(s1!.ref, "default", "没配人设专属参考音 → default");
  assert.strictEqual(req.language, "zh");
  assert.strictEqual(req.emotion, "gentle", "恋爱人设 → gentle");
  assert.strictEqual(req.return_base64, true);

  // 人设专属参考音：CHATX_BOT_VOICE_REF_LOVER 优先；小界仍用默认；路径读不到 → 回落默认（不失败）
  const LOVER_REF = path.join(TMP, "lover.mp3");
  fs.writeFileSync(LOVER_REF, Buffer.from("ID3fake-lover-reference"));
  process.env.CHATX_BOT_VOICE_REF_LOVER = LOVER_REF;
  V.resetVoiceRefCache();
  assert.strictEqual(V.personaRefPath("lover"), LOVER_REF);
  assert.strictEqual(V.personaRefPath("xiaojie"), null);
  hits.length = 0;
  const sl = await V.synthesizeVoice("想你了", "zh", "lover");
  assert.strictEqual(sl?.ref, "persona");
  assert.strictEqual((JSON.parse(String(hits[0].body)) as Record<string, unknown>).reference_audio_b64, fs.readFileSync(LOVER_REF).toString("base64"), "恋爱用专属参考音");
  hits.length = 0;
  const sx = await V.synthesizeVoice("你好", "zh", "xiaojie");
  assert.strictEqual(sx?.ref, "default");
  assert.strictEqual((JSON.parse(String(hits[0].body)) as Record<string, unknown>).reference_audio_b64, fs.readFileSync(REF).toString("base64"), "小界仍用默认");
  process.env.CHATX_BOT_VOICE_REF_LOVER = path.join(TMP, "missing.mp3");
  V.resetVoiceRefCache();
  const origWarn = console.warn;
  const warns: string[] = [];
  console.warn = (...a: unknown[]) => void warns.push(a.join(" "));
  hits.length = 0;
  const sm = await V.synthesizeVoice("想你了", "zh", "lover");
  await V.synthesizeVoice("想你了", "zh", "lover");
  console.warn = origWarn;
  assert.strictEqual(sm?.ref, "default", "专属路径读不到 → 回落默认");
  assert.strictEqual((JSON.parse(String(hits[0].body)) as Record<string, unknown>).reference_audio_b64, fs.readFileSync(REF).toString("base64"));
  assert.strictEqual(warns.length, 1, "回落只告警一次");
  assert.ok(/CHATX_BOT_VOICE_REF_LOVER/.test(warns[0]) && !/missing\.mp3/.test(warns[0]), "告警不带路径 / 内容");
  delete process.env.CHATX_BOT_VOICE_REF_LOVER;
  V.resetVoiceRefCache();
  // 小界默认无情感标签
  hits.length = 0;
  await V.synthesizeVoice("hi", "en");
  assert.strictEqual((JSON.parse(String(hits[0].body)) as Record<string, unknown>).emotion, "", "小界跟参考音");
  // 中继直接给 MP3 也接
  ttsPayload = { audio_base64: Buffer.concat([Buffer.from([0xff, 0xfb, 0x90, 0x00]), Buffer.alloc(100)]).toString("base64") };
  const s2 = await V.synthesizeVoice("x", "zh");
  assert.ok(s2 && s2.mp3[1] === 0xfb, "MP3 原样透传");
  // 失败分支：非 2xx / ok:false / 无音频 / 非 WAV 字节 → null
  ttsStatus = 400;
  ttsPayload = { detail: "no speaker reference" };
  assert.strictEqual(await V.synthesizeVoice("x", "zh"), null, "4xx → null");
  ttsStatus = 200;
  ttsPayload = { ok: false };
  assert.strictEqual(await V.synthesizeVoice("x", "zh"), null, "ok:false → null");
  ttsPayload = { ok: true };
  assert.strictEqual(await V.synthesizeVoice("x", "zh"), null, "无 audio_base64 → null");
  ttsPayload = { ok: true, audio_base64: Buffer.from("garbage-bytes-here").toString("base64") };
  assert.strictEqual(await V.synthesizeVoice("x", "zh"), null, "不是 WAV/MP3 → null");
  // 5xx 主机降权：a 挂 → 自动试 b
  hits.length = 0;
  ttsPayload = { ok: true, audio_base64: makeWav(16000, 1, 0.2).toString("base64") };
  const origFetch = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    if (String(input).startsWith("http://tts-a.test")) {
      hits.push({ url: String(input), headers: {}, body: null });
      return new Response("boom", { status: 503 });
    }
    return origFetch(input, init);
  }) as typeof fetch;
  const s3 = await V.synthesizeVoice("x", "zh");
  globalThis.fetch = origFetch;
  assert.ok(s3, "主中继 5xx 时备机接住");
  assert.deepStrictEqual(hits.map((h) => new URL(h.url).host), ["tts-a.test", "tts-b.test"]);

  // ASR 合同：multipart 到 /v1/audio/transcriptions，file 字段带音频，取 text
  hits.length = 0;
  const ogg = new Uint8Array([0x4f, 0x67, 0x67, 0x53, 1, 2, 3, 4]).buffer;
  const heard = await V.transcribeVoice(ogg, "audio/ogg");
  assert.strictEqual(heard, "能接 WhatsApp 吗", "转写文本 trim");
  assert.strictEqual(hits.length, 1);
  assert.strictEqual(hits[0].url, "http://asr.test/v1/audio/transcriptions");
  assert.match(hits[0].headers["content-type"], /^multipart\/form-data; boundary=/, "multipart 含 boundary");
  assert.strictEqual(hits[0].headers["x-ah-svc"], "svc-token");
  const raw = Buffer.from(hits[0].body as ArrayBuffer).toString("latin1");
  assert.match(raw, /name="file"; filename="voice.ogg"\r\nContent-Type: audio\/ogg/, "file 字段 + 文件名 + MIME");
  assert.ok(raw.includes("OggS"), "音频字节在 body 里");
  assert.match(raw, /name="model"\r\n\r\nwhisper-1/, "OpenAI 形态 model 字段");
  assert.match(raw, /name="prompt"\r\n\r\n[^\r]*ChatX/, "领域词 prompt");
  asrPayload = { text: "   " };
  assert.strictEqual(await V.transcribeVoice(ogg), null, "空转写 → null");
  asrPayload = { error: "bad" };
  assert.strictEqual(await V.transcribeVoice(ogg), null, "无 text → null");
  asrStatus = 400;
  asrPayload = { text: "x" };
  assert.strictEqual(await V.transcribeVoice(ogg), null, "4xx → null");

  console.log("chatx-voice smoke OK");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
