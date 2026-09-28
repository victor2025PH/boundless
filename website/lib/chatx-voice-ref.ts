/**
 * @ChatX_bot 参考音「体检 + 策展」：智聊 voice_ref_health.py / voice_enroll.prepare_reference_audio 的 TS 移植，
 * 规则逐条对齐（同一套阈值与话术），这样 bot 里克隆自己的声音和桌面版登记是同一把尺子：
 *   - 时长：<3s 红（太短）· 3–5s 黄（略短）· 5–18s 绿 · >18s 黄（只取前 20s）
 *   - 削波：满幅（≥0.99）样本 >2% 红 · >0.5% 黄
 *   - 静音：20ms 帧 RMS < 峰值 2% 的占比 >70% 红 · >50% 黄
 *   - 峰值 <1e-3 → 几乎无声（红）
 * 策展：去首尾静音 → 超长裁到 CURATE_MAX_SEC → 单声道 16-bit WAV。纯函数、无 IO，可单测。
 */

export const DUR_MIN = 3.0;
export const DUR_LOW = 5.0;
export const DUR_HIGH = 18.0;
export const DUR_MAX = 20.0;
/** 登记用参考音上限：与 voice_enroll 的「过长素材裁到最佳窗」同意；15s 足够 IndexTTS-2 抓音色。 */
export const CURATE_MAX_SEC = 15.0;

export type Grade = "green" | "yellow" | "red" | "unknown";

export interface RefHealth {
  grade: Grade;
  score: number;
  summary: string;
  duration_sec: number;
  clip_ratio: number;
  silence_ratio: number;
  peak_dbfs: number;
  noise_floor_dbfs: number;
  issues: string[];
  hints: string[];
}

const RANK: Record<"green" | "yellow" | "red", number> = { green: 0, yellow: 1, red: 2 };
const SUMMARY: Record<Grade, string> = {
  green: "质量良好，适合克隆",
  yellow: "可用，按提示优化会更像",
  red: "质量不佳，建议按提示重录",
  unknown: "无法评估",
};
const GREEN_TIP = "已不错～环境越安静、单人清晰朗读，克隆越像";

function dbfs(x: number): number {
  return Math.round(20 * Math.log10(Math.max(x, 1e-9)) * 10) / 10;
}
const r = (x: number, d: number) => Math.round(x * 10 ** d) / 10 ** d;

function blank(grade: Grade): RefHealth {
  return {
    grade,
    score: grade === "green" ? 100 : 0,
    summary: SUMMARY[grade],
    duration_sec: 0,
    clip_ratio: 0,
    silence_ratio: 0,
    peak_dbfs: -120,
    noise_floor_dbfs: -120,
    issues: [],
    hints: [],
  };
}

function frameRms(x: Float32Array, sr: number): Float64Array {
  const fl = Math.max(1, Math.floor(sr * 0.02));
  const nf = Math.floor(x.length / fl);
  if (nf < 1) {
    let s = 0;
    for (const v of x) s += v * v;
    return Float64Array.of(Math.sqrt(s / Math.max(1, x.length)));
  }
  const out = new Float64Array(nf);
  for (let f = 0; f < nf; f++) {
    let s = 0;
    for (let i = f * fl; i < (f + 1) * fl; i++) s += x[i] * x[i];
    out[f] = Math.sqrt(s / fl);
  }
  return out;
}

/** 对单声道浮点样本（[-1,1]）体检；与 voice_ref_health.analyze_reference_audio 同 schema、同阈值，绝不抛。 */
export function analyzeReference(samples: Float32Array, sr: number, maxLenSec = DUR_MAX): RefHealth {
  try {
    const n = samples.length;
    if (n === 0 || !(sr > 0)) {
      const o = blank("red");
      o.issues = ["空音频"];
      o.hints = ["没读到有效音频，换个文件再试"];
      o.summary = "无法读取音频";
      return o;
    }
    let peak = 0;
    let clipped = 0;
    for (let i = 0; i < n; i++) {
      const v = Number.isFinite(samples[i]) ? Math.min(1, Math.max(-1, samples[i])) : 0;
      const a = Math.abs(v);
      if (a > peak) peak = a;
      if (a >= 0.99) clipped++;
    }
    const dur = n / sr;
    if (peak < 1e-3) {
      const o = blank("red");
      o.duration_sec = r(dur, 1);
      o.silence_ratio = 1;
      o.peak_dbfs = dbfs(peak);
      o.issues = ["几乎无声"];
      o.hints = ["这段几乎没有声音，确认录到人声再上传"];
      return o;
    }
    const clipRatio = clipped / n;
    const frms = frameRms(samples, sr);
    const silThresh = peak * 0.02;
    let sil = 0;
    for (const v of frms) if (v < silThresh) sil++;
    const silenceRatio = sil / frms.length;
    const sorted = Array.from(frms).sort((a, b) => a - b);
    const quietN = Math.max(1, Math.floor(sorted.length * 0.1));
    const noiseFloor = sorted.slice(0, quietN).reduce((a, b) => a + b, 0) / quietN;

    const checks: Array<["yellow" | "red", string, string]> = [];
    if (dur < DUR_MIN) checks.push(["red", "录音过短", "太短了，建议 6–15 秒连续清晰人声，克隆才稳"]);
    else if (dur < DUR_LOW) checks.push(["yellow", "录音略短", "建议 8–15 秒，采样更足、音色更稳"]);
    else if (dur > DUR_HIGH) checks.push(["yellow", "录音偏长", `只会用前 ${Math.floor(maxLenSec)} 秒，8–15 秒即可`]);
    if (clipRatio > 0.02) checks.push(["red", "削波破音", "录音过载有破音，调低录音电平/离麦远点重录"]);
    else if (clipRatio > 0.005) checks.push(["yellow", "轻微削波", "音量偏大略有破音，下次小声一点"]);
    if (silenceRatio > 0.7) checks.push(["red", "有效人声过少", "大段静音/留白，剪掉空白、多保留说话"]);
    else if (silenceRatio > 0.5) checks.push(["yellow", "静音偏多", "句间留白有点多，剪短停顿会更好"]);

    let grade: "green" | "yellow" | "red" = "green";
    const issues: string[] = [];
    const hints: string[] = [];
    for (const [g, iss, h] of checks) {
      issues.push(iss);
      hints.push(h);
      if (RANK[g] > RANK[grade]) grade = g;
    }
    if (grade === "green" && hints.length === 0) hints.push(GREEN_TIP);
    const nred = checks.filter((c) => c[0] === "red").length;
    const nyel = checks.length - nred;
    return {
      grade,
      score: Math.max(0, Math.min(100, 100 - 45 * nred - 17 * nyel)),
      summary: SUMMARY[grade],
      duration_sec: r(dur, 1),
      clip_ratio: r(clipRatio, 4),
      silence_ratio: r(silenceRatio, 3),
      peak_dbfs: dbfs(peak),
      noise_floor_dbfs: dbfs(noiseFloor),
      issues,
      hints,
    };
  } catch {
    return blank("unknown");
  }
}

/** 去首尾静音（帧 RMS < 峰值 2%，两端各留 0.1s）并裁到 maxSec。削波须在原始音上测，所以体检在策展前做。 */
export function curateReference(samples: Float32Array, sr: number, maxSec = CURATE_MAX_SEC): Float32Array {
  if (samples.length === 0 || !(sr > 0)) return samples;
  const fl = Math.max(1, Math.floor(sr * 0.02));
  const frms = frameRms(samples, sr);
  let peak = 0;
  for (const v of samples) peak = Math.max(peak, Math.abs(v));
  const th = peak * 0.02;
  let a = 0;
  let b = frms.length - 1;
  while (a < frms.length && frms[a] < th) a++;
  while (b > a && frms[b] < th) b--;
  if (a >= frms.length) return samples;
  const pad = Math.floor(sr * 0.1);
  const s0 = Math.max(0, a * fl - pad);
  const s1 = Math.min(samples.length, (b + 1) * fl + pad, s0 + Math.floor(maxSec * sr));
  return samples.subarray(s0, s1);
}

/** 16-bit PCM WAV（任意声道）→ 单声道 Float32；不是 16-bit PCM 返回 null。 */
export function decodeWavMono(buf: Buffer): { samples: Float32Array; sampleRate: number } | null {
  if (buf.length < 12 || buf.toString("ascii", 0, 4) !== "RIFF" || buf.toString("ascii", 8, 12) !== "WAVE") return null;
  let off = 12;
  let fmt: { format: number; channels: number; sampleRate: number; bits: number } | null = null;
  while (off + 8 <= buf.length) {
    const id = buf.toString("ascii", off, off + 4);
    const size = buf.readUInt32LE(off + 4);
    const body = off + 8;
    if (id === "fmt " && size >= 16) {
      fmt = { format: buf.readUInt16LE(body), channels: buf.readUInt16LE(body + 2), sampleRate: buf.readUInt32LE(body + 4), bits: buf.readUInt16LE(body + 14) };
    } else if (id === "data") {
      if (!fmt || fmt.format !== 1 || fmt.bits !== 16 || fmt.channels < 1) return null;
      const end = Math.min(buf.length, body + size);
      const frames = Math.floor((end - body) / (2 * fmt.channels));
      const out = new Float32Array(frames);
      for (let i = 0; i < frames; i++) {
        let s = 0;
        for (let c = 0; c < fmt.channels; c++) s += buf.readInt16LE(body + (i * fmt.channels + c) * 2);
        out[i] = s / fmt.channels / 32768;
      }
      return { samples: out, sampleRate: fmt.sampleRate };
    }
    off = body + size + (size & 1);
  }
  return null;
}

/** 整数倍降采样（块平均当简易低通）；factor ≤ 1 原样返回。 */
export function downsample(samples: Float32Array, factor: number): Float32Array {
  const f = Math.floor(factor);
  if (f <= 1) return samples;
  const out = new Float32Array(Math.floor(samples.length / f));
  for (let i = 0; i < out.length; i++) {
    let s = 0;
    for (let k = 0; k < f; k++) s += samples[i * f + k];
    out[i] = s / f;
  }
  return out;
}

export function encodeWavMono(samples: Float32Array, sampleRate: number): Buffer {
  const data = Buffer.alloc(samples.length * 2);
  for (let i = 0; i < samples.length; i++) {
    const v = Math.max(-1, Math.min(1, samples[i]));
    data.writeInt16LE(Math.round(v < 0 ? v * 32768 : v * 32767), i * 2);
  }
  const h = Buffer.alloc(44);
  h.write("RIFF", 0, "ascii");
  h.writeUInt32LE(36 + data.length, 4);
  h.write("WAVE", 8, "ascii");
  h.write("fmt ", 12, "ascii");
  h.writeUInt32LE(16, 16);
  h.writeUInt16LE(1, 20);
  h.writeUInt16LE(1, 22);
  h.writeUInt32LE(sampleRate, 24);
  h.writeUInt32LE(sampleRate * 2, 28);
  h.writeUInt16LE(2, 32);
  h.writeUInt16LE(16, 34);
  h.write("data", 36, "ascii");
  h.writeUInt32LE(data.length, 40);
  return Buffer.concat([h, data]);
}
