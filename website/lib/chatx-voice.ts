/**
 * @ChatX_bot 语音：让 bot 像 ChatX 桌面版一样「听得懂语音、能回语音」。
 *
 *   听：Telegram voice（OGG/OPUS）→ getFile 下载 → ASR 中继（whisper，/v1/audio/transcriptions）→ 文字进 handleFreeText
 *   说：AI 回答文字 → TTS 中继（IndexTTS-2 /v1/tts/clone，需参考音）→ WAV → MP3（lamejs，纯 JS 无 ffmpeg）→ sendVoice
 *
 * 参考音色：CHATX_BOT_VOICE_REF，缺省用官网「真实样片」目录里的授权真人模板 clone-original.mp3
 * （AISHELL-3 SSB0139，项目标准音色库；见 public/showcase/real/README.md）——不静默拿别的 demo 音频当人声。
 * 人设可各配一条：CHATX_BOT_VOICE_REF_LOVER / _SALES / _XIAOJIE（大写人设名），没配或读不到就回落默认参考音。
 * 用户可按人设从音色库选音色、或克隆自己的声音（chatx-voicepack / chatx-voice-clone）；解析顺序见 referenceAudio。
 *
 * 何时回语音（wantsVoiceReply）：用户 /voice 显式开关优先；没设过 → 恋爱陪聊人设默认开、
 * 用户发语音就回语音，其余仍只回文字（不改变已有纯文字体验）。GPU 合成串行 4–5 秒一句，
 * 所以有 单人冷却 / 单人日配额 / 全局日配额 三道闸，TTS 失败只丢语音、文字已先发出。
 */
import { readFile } from "fs/promises";
import path from "path";
import { asrRelayEnabled, proxyAsr, proxyTts, ttsRelayEnabled } from "./ai-gateway";
import type { BotLang } from "./bot-knowledge";
import type { ChatxPersona, ChatxPrefs } from "./chatx-prefs";
import { DEFAULT_VOICE, MY_VOICE, resolveVoiceChoice, voicepackPath } from "./chatx-voicepack";

export const VOICE_TEXT_MAX = 240;
export const VOICE_IN_MAX_SEC = 60;
export const VOICE_IN_MAX_BYTES = 2 * 1024 * 1024;
const TTS_TIMEOUT_MS = Number(process.env.CHATX_VOICE_TTS_TIMEOUT_MS ?? 25_000);
const ASR_TIMEOUT_MS = Number(process.env.CHATX_VOICE_ASR_TIMEOUT_MS ?? 20_000);
const USER_COOLDOWN_MS = Number(process.env.CHATX_VOICE_COOLDOWN_MS ?? 12_000);
const USER_DAILY_MAX = Number(process.env.CHATX_VOICE_USER_DAILY_MAX ?? 30);
const GLOBAL_DAILY_MAX = Number(process.env.CHATX_VOICE_DAILY_MAX ?? 600);
const MP3_KBPS = 48;
const REF_PATH = process.env.CHATX_BOT_VOICE_REF || path.join(process.cwd(), "public", "showcase", "real", "clone-original.mp3");

/** 人设 → IndexTTS-2 情感标签（空 = 跟参考音）。 */
const EMOTION: Record<ChatxPersona, string> = { xiaojie: "", lover: "gentle", sales: "" };

export function voiceOutEnabled(): boolean {
  return ttsRelayEnabled();
}
export function voiceInEnabled(): boolean {
  return asrRelayEnabled();
}

export function wantsVoiceReply(prefs: ChatxPrefs, incomingVoice: boolean): boolean {
  if (typeof prefs.voice === "boolean") return prefs.voice;
  return prefs.persona === "lover" || incomingVoice;
}

// ── 配额闸 ──────────────────────────────────────────────────────────

export type VoiceDeny = "disabled" | "empty" | "too_long" | "cooldown" | "user_quota" | "global_quota";

const lastVoiceAt = new Map<number, number>();
let quotaDay = "";
let globalCount = 0;
const userCount = new Map<number, number>();

function rollDay(now: number) {
  const d = new Date(now).toISOString().slice(0, 10);
  if (d !== quotaDay) {
    quotaDay = d;
    globalCount = 0;
    userCount.clear();
    lastVoiceAt.clear();
  }
}

/** 通过则占用一次额度（合成失败不退——GPU 时间已经花了）。 */
export function takeVoiceSlot(uid: number, textLen: number, now = Date.now()): VoiceDeny | null {
  if (!voiceOutEnabled()) return "disabled";
  if (textLen <= 0) return "empty";
  if (textLen > VOICE_TEXT_MAX) return "too_long";
  rollDay(now);
  if (now - (lastVoiceAt.get(uid) ?? 0) < USER_COOLDOWN_MS) return "cooldown";
  if ((userCount.get(uid) ?? 0) >= USER_DAILY_MAX) return "user_quota";
  if (globalCount >= GLOBAL_DAILY_MAX) return "global_quota";
  lastVoiceAt.set(uid, now);
  userCount.set(uid, (userCount.get(uid) ?? 0) + 1);
  globalCount++;
  return null;
}

/** 测试用：清空配额状态。 */
export function resetVoiceQuota() {
  quotaDay = "";
  rollDay(Date.now());
}

// ── 文本清洗 ────────────────────────────────────────────────────────

/** 朗读用文本：去链接 / emoji / Markdown 符号 / 多余空白；链接与按钮由文字消息承接。 */
export function ttsText(raw: string): string {
  return raw
    .replace(/https?:\/\/\S+/g, "")
    .replace(/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{FE0F}\u{200D}\u{2B50}\u{2B06}\u{2194}-\u{21AA}]/gu, "")
    .replace(/[*_`#>|~\[\]()【】「」]/g, "")
    .replace(/[ \t]+/g, " ")
    .replace(/\s*\n\s*/g, "\n")
    .trim();
}

/** 用户这句话是否在明确要语音（"发条语音""用声音说""send a voice note"）。 */
export function asksForVoice(text: string): boolean {
  return /(发|来|用|说|回|听|讲)[^，,。？?！!]{0,4}(语音|声音)|语音(回|说|聊|发|讲)|voice\s*(message|note|reply|msg)|send\s+(me\s+)?(a\s+)?voice|speak\s+to\s+me|say\s+it\s+out\s+loud/i.test(text);
}

const VOICE_WORD_RE = /语音|声音|voice|audio/i;
const DENIAL_RE = /发不了|不能发|无法发|没法发|发不出|暂时(还)?(不能|没法|无法)|can'?t|cannot|unable|not able|only\s+(reply|respond|send|do)?\s*(in\s+)?text/i;
const TEXT_ONLY_RE = /只能(用|发|回|打)?文字|只能打字|text[- ]only|only\s+(reply|respond|chat)\s+(in|by|with)\s+text/i;
const LEAD_CONNECTOR_RE = /^\s*(不过呢?|但是?|可是|However,?|But)\s*/i;

/**
 * 语音会随回复一起发时，删掉模型"我只能文字 / 发不了语音"这类自相矛盾的分句（逗号级），并去掉紧跟的"不过 / 但"。
 * 全删光则原样返回（宁可不删也不发空消息）。
 */
export function scrubVoiceDenial(text: string): string {
  const parts = text.split(/(?<=[，,。！？!?；;\n]|\.\s)/);
  const kept: string[] = [];
  let dropped = false;
  let pad = "";
  for (const p of parts) {
    if ((VOICE_WORD_RE.test(p) && DENIAL_RE.test(p)) || TEXT_ONLY_RE.test(p)) {
      if (!dropped) pad = p.trimStart() !== p ? " " : "";
      dropped = true;
      continue;
    }
    kept.push(dropped ? pad + p.replace(LEAD_CONNECTOR_RE, "") : p);
    dropped = false;
  }
  const out = kept.join("").trim();
  return out || text;
}

/** 系统提示里的语音能力声明：on = 这条会配语音；off = 只发文字但可用 /voice 打开。 */
export function voiceReplyHint(lang: BotLang, on: boolean): string {
  if (lang === "zh") {
    return on
      ? "【语音】你这条回复会由系统自动用你当前人设的声音合成语音，和文字一起发给对方——你能发语音。绝不要说自己只能文字回复 / 发不了语音；历史里如果这样说过，那是旧状态，以现在为准。用适合朗读的口语短句。对方想换声音发 /voices，想用自己的声音发 /clone，不想听发 /voice 关掉。"
      : "【语音】对方现在关着语音回复，这条只发文字。对方想听语音时告诉他发 /voice 打开（你能发语音，不要说做不到）；换声音发 /voices，用自己的声音发 /clone。";
  }
  return on
    ? "[Voice] The system will automatically speak this reply in your current persona's voice and send it together with the text — you CAN send voice. Never say you can only reply in text or can't send voice; if earlier turns said so, that is outdated. Write short, speakable sentences. /voices changes the voice, /clone uses their own voice, /voice turns voice off."
    : "[Voice] The user has voice replies turned off, so this one is text only. If they want to hear you, tell them to send /voice (you can speak — never say you can't); /voices changes the voice, /clone uses their own voice.";
}

// ── WAV → MP3 ──────────────────────────────────────────────────────

type Wav = { sampleRate: number; channels: number; pcm: Int16Array };

/** 只认 16-bit PCM（IndexTTS-2 / emotion_tts 中继都吐这个）；其他格式返回 null 让上层放弃语音。 */
export function parseWav(buf: Buffer): Wav | null {
  if (buf.length < 12 || buf.toString("ascii", 0, 4) !== "RIFF" || buf.toString("ascii", 8, 12) !== "WAVE") return null;
  let off = 12;
  let fmt: { format: number; channels: number; sampleRate: number; bits: number } | null = null;
  while (off + 8 <= buf.length) {
    const id = buf.toString("ascii", off, off + 4);
    const size = buf.readUInt32LE(off + 4);
    const body = off + 8;
    if (id === "fmt " && size >= 16) {
      fmt = {
        format: buf.readUInt16LE(body),
        channels: buf.readUInt16LE(body + 2),
        sampleRate: buf.readUInt32LE(body + 4),
        bits: buf.readUInt16LE(body + 14),
      };
    } else if (id === "data") {
      if (!fmt || fmt.format !== 1 || fmt.bits !== 16 || fmt.channels < 1) return null;
      const end = Math.min(buf.length, body + size);
      const bytes = (end - body) & ~1;
      const pcm = new Int16Array(bytes / 2);
      for (let i = 0; i < pcm.length; i++) pcm[i] = buf.readInt16LE(body + i * 2);
      return { sampleRate: fmt.sampleRate, channels: fmt.channels, pcm };
    }
    off = body + size + (size & 1);
  }
  return null;
}

/** lamejs 只有 ESM 入口能拿到 Mp3Encoder（CJS 入口是不导出的 IIFE）：动态 import 让 Next 与 tsx 都走 ESM。 */
let lame: Promise<typeof import("@breezystack/lamejs")> | null = null;
function encoder(): Promise<typeof import("@breezystack/lamejs")> {
  lame ??= import("@breezystack/lamejs");
  return lame;
}

export async function wavToMp3(wav: Buffer, kbps = MP3_KBPS): Promise<{ mp3: Buffer; sampleRate: number; durationSec: number } | null> {
  const w = parseWav(wav);
  if (!w || w.pcm.length === 0) return null;
  let mono = w.pcm;
  if (w.channels > 1) {
    const frames = Math.floor(w.pcm.length / w.channels);
    mono = new Int16Array(frames);
    for (let i = 0; i < frames; i++) {
      let s = 0;
      for (let c = 0; c < w.channels; c++) s += w.pcm[i * w.channels + c];
      mono[i] = Math.round(s / w.channels);
    }
  }
  const { Mp3Encoder } = await encoder();
  const enc = new Mp3Encoder(1, w.sampleRate, kbps);
  const parts: Uint8Array[] = [];
  const BLOCK = 1152;
  for (let i = 0; i < mono.length; i += BLOCK) {
    const chunk = enc.encodeBuffer(mono.subarray(i, i + BLOCK));
    if (chunk.length) parts.push(chunk);
  }
  const tail = enc.flush();
  if (tail.length) parts.push(tail);
  return { mp3: Buffer.concat(parts.map((p) => Buffer.from(p))), sampleRate: w.sampleRate, durationSec: mono.length / w.sampleRate };
}

// ── TTS ────────────────────────────────────────────────────────────

/** 人设专属参考音路径（环境变量 CHATX_BOT_VOICE_REF_<PERSONA>）；没配返回 null。 */
export function personaRefPath(persona: ChatxPersona): string | null {
  const v = process.env[`CHATX_BOT_VOICE_REF_${persona.toUpperCase()}`];
  return v && v.trim() ? v.trim() : null;
}

const refCache = new Map<string, Promise<string | null>>();
/** 只缓存读成功的（库文件可能稍后才抓到）；用户克隆音不走缓存（可随时删除 / 重录）。 */
function readRef(p: string): Promise<string | null> {
  let c = refCache.get(p);
  if (!c) {
    c = readFile(p)
      .then((b) => b.toString("base64"))
      .catch(() => {
        refCache.delete(p);
        return null;
      });
    refCache.set(p, c);
  }
  return c;
}

export type RefSource = "mine" | "lib" | "persona" | "default";

let warnedPersonaRef = new Set<string>();
/**
 * 参考音解析：用户对该人设的显式选择（我的克隆音 / 库音色 / 默认）→ 运维人设参考音（环境变量）→ 默认参考音。
 * 任一环读不到就往下回落，语音永远有声。
 */
export async function referenceAudio(persona: ChatxPersona = "xiaojie", prefs: ChatxPrefs = {}): Promise<{ b64: string; ref: RefSource; voice?: string } | null> {
  const { choice, explicit } = resolveVoiceChoice(prefs, persona);
  if (explicit && choice === MY_VOICE && prefs.myVoice?.path) {
    const b64 = await readFile(prefs.myVoice.path).then((b) => b.toString("base64")).catch(() => null);
    if (b64) return { b64, ref: "mine", voice: MY_VOICE };
  } else if (explicit && choice !== DEFAULT_VOICE && choice !== MY_VOICE) {
    const b64 = await readRef(voicepackPath(choice));
    if (b64) return { b64, ref: "lib", voice: choice };
  }
  const envRef = explicit ? null : personaRefPath(persona);
  if (envRef) {
    const b64 = await readRef(envRef);
    if (b64) return { b64, ref: "persona" };
    if (!warnedPersonaRef.has(persona)) {
      warnedPersonaRef.add(persona);
      console.warn(`[chatx-voice] CHATX_BOT_VOICE_REF_${persona.toUpperCase()} unreadable, falling back to default reference`);
    }
  }
  const b64 = await readRef(REF_PATH);
  return b64 ? { b64, ref: "default" } : null;
}

/** 测试用：清参考音缓存。 */
export function resetVoiceRefCache(): void {
  refCache.clear();
  warnedPersonaRef = new Set();
}

export type Synth = { mp3: Buffer; durationSec: number; ms: number; ref: RefSource; voice?: string };

export async function synthesizeVoice(text: string, lang: BotLang, persona: ChatxPersona = "xiaojie", prefs: ChatxPrefs = {}): Promise<Synth | null> {
  const started = Date.now();
  const r = await referenceAudio(persona, prefs);
  if (!r) return null;
  const ref = r.b64;
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), TTS_TIMEOUT_MS);
  try {
    const res = await proxyTts(
      "/v1/tts/clone",
      JSON.stringify({ text, reference_audio_b64: ref, language: lang, emotion: EMOTION[persona], return_base64: true }),
      ac.signal
    );
    if (!res.ok) return null;
    const j = (await res.json()) as { ok?: boolean; audio_base64?: string };
    if (j.ok === false || !j.audio_base64) return null;
    const audio = Buffer.from(j.audio_base64, "base64");
    // 中继按合同吐 WAV；万一将来直接给 MP3（ID3 或帧同步头）就原样用
    if (audio.toString("ascii", 0, 3) === "ID3" || (audio[0] === 0xff && (audio[1] & 0xe0) === 0xe0)) {
      return { mp3: audio, durationSec: 0, ms: Date.now() - started, ref: r.ref, voice: r.voice };
    }
    const out = await wavToMp3(audio);
    return out ? { mp3: out.mp3, durationSec: out.durationSec, ms: Date.now() - started, ref: r.ref, voice: r.voice } : null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

// ── ASR ────────────────────────────────────────────────────────────

/** OpenAI 形态 multipart；prompt 给 whisper 领域词（品牌名别转成「小姐」）。 */
export async function transcribeVoice(audio: ArrayBuffer, mime = "audio/ogg", filename = "voice.ogg"): Promise<string | null> {
  if (!voiceInEnabled()) return null;
  const form = new FormData();
  form.append("file", new Blob([audio], { type: mime }), filename);
  form.append("model", "whisper-1");
  form.append("response_format", "json");
  form.append("prompt", "ChatX 智聊 小界 AI 全自动聊天 Telegram WhatsApp LINE Messenger 微信");
  const req = new Request("http://relay.local/asr", { method: "POST", body: form });
  const body = await req.arrayBuffer();
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), ASR_TIMEOUT_MS);
  try {
    const res = await proxyAsr(body, req.headers.get("content-type") || "", ac.signal);
    if (!res.ok) return null;
    const j = (await res.json()) as { text?: unknown };
    const text = typeof j.text === "string" ? j.text.trim() : "";
    return text || null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}
