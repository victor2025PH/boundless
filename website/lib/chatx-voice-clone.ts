import { mkdir, unlink, writeFile } from "fs/promises";
import path from "path";
import type { BotLang } from "./bot-knowledge";
import type { ChatxMyVoice } from "./chatx-prefs";
import { analyzeReference, curateReference, decodeWavMono, encodeWavMono, downsample, type RefHealth } from "./chatx-voice-ref";
import { USER_VOICE_DIR } from "./chatx-voicepack";

/**
 * 用户克隆自己的声音：走智聊「局域网零样本」登记语义（voice_enroll.build_lan_voice_profile）——
 * 不上云注册音色 ID，只把体检合格、策展后的参考音存到服务器私有目录，合成时作为 reference_audio 交给 IndexTTS-2。
 *   ① 必须先点「本人声音、同意克隆」（owner_consent），10 分钟内发来的语音才当样本；
 *   ② Telegram 语音（OGG/Opus）用 wasm 解码，WAV 直接读；
 *   ③ voice_ref_health 同阈值体检：红灯拒收并给可执行建议，黄 / 绿登记；
 *   ④ 去首尾静音、裁到 15s、降到 24kHz 单声道 WAV 落盘；新样本成功后才替换旧的（失败不动旧克隆，同 voice_profile_guard）。
 */

export const CLONE_ARM_MS = 10 * 60_000;
export const CLONE_IN_MAX_SEC = 60;
export const CLONE_IN_MAX_BYTES = 3 * 1024 * 1024;

export function cloneArmed(armedAt: string | undefined, now = Date.now()): boolean {
  if (!armedAt) return false;
  const t = Date.parse(armedAt);
  return Number.isFinite(t) && now - t >= 0 && now - t < CLONE_ARM_MS;
}

type OggDecoder = { ready: Promise<void>; decodeFile: (d: Uint8Array) => Promise<{ channelData: Float32Array[]; samplesDecoded: number; sampleRate: number }>; free: () => void };
let oggMod: Promise<{ OggOpusDecoder: new () => OggDecoder }> | null = null;

/** OGG/Opus 或 16-bit WAV → 单声道 Float32；解不出返回 null。 */
export async function decodeAudio(buf: Buffer): Promise<{ samples: Float32Array; sampleRate: number } | null> {
  const wav = decodeWavMono(buf);
  if (wav) return wav;
  if (buf.length < 4 || buf.toString("ascii", 0, 4) !== "OggS") return null;
  try {
    oggMod ??= import("ogg-opus-decoder") as unknown as Promise<{ OggOpusDecoder: new () => OggDecoder }>;
    const { OggOpusDecoder } = await oggMod;
    const dec = new OggOpusDecoder();
    await dec.ready;
    try {
      const out = await dec.decodeFile(new Uint8Array(buf));
      if (!out.samplesDecoded || !out.channelData.length) return null;
      const ch = out.channelData;
      if (ch.length === 1) return { samples: ch[0].subarray(0, out.samplesDecoded), sampleRate: out.sampleRate };
      const mono = new Float32Array(out.samplesDecoded);
      for (let i = 0; i < mono.length; i++) {
        let s = 0;
        for (const c of ch) s += c[i];
        mono[i] = s / ch.length;
      }
      return { samples: mono, sampleRate: out.sampleRate };
    } finally {
      dec.free();
    }
  } catch {
    return null;
  }
}

export type CloneResult =
  | { ok: true; voice: ChatxMyVoice; health: RefHealth }
  | { ok: false; code: "not_decodable" | "rejected"; health?: RefHealth };

/** 体检 + 策展 + 落盘；不写 prefs（由调用方在成功后写，失败保留旧克隆）。 */
export async function enrollUserVoice(uid: number, audio: Buffer, consentAt: string, now = new Date()): Promise<CloneResult> {
  const d = await decodeAudio(audio);
  if (!d) return { ok: false, code: "not_decodable" };
  const health = analyzeReference(d.samples, d.sampleRate);
  if (health.grade === "red" || health.grade === "unknown") return { ok: false, code: "rejected", health };
  let seg = curateReference(d.samples, d.sampleRate);
  let sr = d.sampleRate;
  if (sr === 48000) {
    seg = downsample(seg, 2);
    sr = 24000;
  }
  await mkdir(USER_VOICE_DIR, { recursive: true });
  const file = path.join(USER_VOICE_DIR, `${uid}-${now.getTime()}.wav`);
  await writeFile(file, encodeWavMono(seg, sr), { mode: 0o600 });
  return {
    ok: true,
    health,
    voice: {
      path: file,
      sec: Math.round((seg.length / sr) * 10) / 10,
      grade: health.grade,
      score: health.score,
      owner_consent: true,
      consentAt,
      createdAt: now.toISOString(),
    },
  };
}

/** 只删 USER_VOICE_DIR 下的文件（防 prefs 里被塞了别的路径）。 */
export async function removeUserVoiceFile(p: string | undefined): Promise<void> {
  if (!p) return;
  const abs = path.resolve(p);
  if (path.dirname(abs) !== path.resolve(USER_VOICE_DIR)) return;
  await unlink(abs).catch(() => {});
}

export function cloneRejectText(r: Extract<CloneResult, { ok: false }>, lang: BotLang): string {
  const zh = lang === "zh";
  if (r.code === "not_decodable" || !r.health) {
    return zh ? "🎙 这段音频我解不出来——请直接按住麦克风录一条 Telegram 语音再发。" : "🎙 I couldn't decode that audio — please hold the mic and record a Telegram voice message.";
  }
  const h = r.health;
  const tips = h.hints.map((t) => `• ${t}`).join("\n");
  return zh
    ? `🔴 这段样本不太适合克隆（${h.issues.join("、") || h.summary}，${h.duration_sec}s）。\n${tips}\n\n再录一条发给我就行（10 分钟内有效）。`
    : `🔴 This sample isn't good enough to clone (${h.issues.join(", ") || "low quality"}, ${h.duration_sec}s).\nTips: record 8–15s of continuous speech in a quiet room, not too loud.\n\nJust send another one (valid for 10 minutes).`;
}
