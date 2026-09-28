/**
 * 把 @ChatX_bot 音色库的参考音落到服务器（VOICEPACK_DIR）。流程同智聊 avatarhub/tools/_fetch_aishell3_refs.py：
 * 从 Hugging Face 抓 AISHELL-3 说话人（train/test 两个分区都找）前若干条棚录 → 每条去首尾静音 → 0.25s 间隔拼到 10–15s → 44.1k 降到 22.05k
 * → 用 chatx-voice-ref 体检（红灯不落盘）。已存在的跳过（--force 重做）。只新增文件，不删任何东西。
 * 运行：npx tsx scripts/chatx-voicepack-fetch.ts [--force] [SSB0016 ...]
 */
import { mkdir, stat, writeFile } from "fs/promises";
import { analyzeReference, curateReference, decodeWavMono, downsample, encodeWavMono } from "../lib/chatx-voice-ref";
import { VOICEPACK, VOICEPACK_DIR, voicepackPath } from "../lib/chatx-voicepack";

const HF = process.env.HF_ENDPOINT || "https://huggingface.co";
const REPO = "datasets/shenyunhang/AISHELL-3";
const TARGET_SEC = 10;
const MAX_SEC = 15;
const GAP_SEC = 0.25;
const MAX_UTT = 12;

async function get(url: string): Promise<Response> {
  const res = await fetch(url, { headers: { "User-Agent": "chatx-voicepack/1.0" }, signal: AbortSignal.timeout(60_000) });
  if (!res.ok) throw new Error(`${res.status} ${url}`);
  return res;
}

async function buildRef(spk: string): Promise<{ wav: Buffer; sec: number; grade: string; score: number; utts: number }> {
  let tree: Array<{ type: string; path: string }> = [];
  for (const split of ["train", "test"]) {
    const res = await fetch(`${HF}/api/${REPO}/tree/main/${split}/wav/${spk}`, { signal: AbortSignal.timeout(60_000) });
    if (res.ok) {
      tree = (await res.json()) as Array<{ type: string; path: string }>;
      break;
    }
  }
  const names = tree.filter((t) => t.type === "file" && t.path.endsWith(".wav")).map((t) => t.path).sort().slice(0, MAX_UTT);
  const parts: Float32Array[] = [];
  let sr = 0;
  let total = 0;
  let utts = 0;
  for (const p of names) {
    const buf = Buffer.from(await (await get(`${HF}/${REPO}/resolve/main/${p}`)).arrayBuffer());
    const d = decodeWavMono(buf);
    if (!d || (sr && d.sampleRate !== sr)) continue;
    sr = d.sampleRate;
    const seg = curateReference(d.samples, sr, 30);
    if (parts.length) parts.push(new Float32Array(Math.floor(GAP_SEC * sr)));
    parts.push(seg);
    total += seg.length / sr + (utts ? GAP_SEC : 0);
    utts++;
    if (total >= TARGET_SEC) break;
  }
  if (!sr || !parts.length) throw new Error("no usable utterances");
  let all = new Float32Array(parts.reduce((n, a) => n + a.length, 0));
  let o = 0;
  for (const a of parts) {
    all.set(a, o);
    o += a.length;
  }
  all = all.subarray(0, Math.min(all.length, Math.floor(MAX_SEC * sr)));
  const factor = sr >= 44100 ? 2 : 1;
  const out = downsample(all, factor);
  const outSr = Math.round(sr / factor);
  const h = analyzeReference(out, outSr);
  if (h.grade === "red") throw new Error(`health red: ${h.issues.join(",")}`);
  return { wav: encodeWavMono(out, outSr), sec: h.duration_sec, grade: h.grade, score: h.score, utts };
}

async function main() {
  const args = process.argv.slice(2);
  const force = args.includes("--force");
  const only = args.filter((a) => /^SSB\d{4}$/.test(a));
  await mkdir(VOICEPACK_DIR, { recursive: true });
  let fail = 0;
  for (const v of VOICEPACK) {
    if (only.length && !only.includes(v.spk)) continue;
    const dst = voicepackPath(v.spk);
    if (!force && (await stat(dst).catch(() => null))) {
      console.log(`${v.spk} ${v.zh}: exists, skip`);
      continue;
    }
    try {
      const r = await buildRef(v.spk);
      await writeFile(dst, r.wav);
      console.log(`${v.spk} ${v.zh}: ${r.sec}s ${r.grade}/${r.score} from ${r.utts} utts → ${dst}`);
    } catch (e) {
      fail++;
      console.error(`${v.spk} ${v.zh}: FAIL ${(e as Error).message}`);
    }
  }
  process.exit(fail ? 1 : 0);
}

void main();
