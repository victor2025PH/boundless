/**
 * 音色库选择 + 用户克隆（纯逻辑）：参考音体检阈值（同智聊 voice_ref_health）、选择解析顺序、
 * referenceAudio 回落链、克隆登记 / 拒收、删除路径防护。运行：npx tsx lib/chatx-voicepack.test.ts
 */
import assert from "assert";
import fs from "fs";
import os from "os";
import path from "path";

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "chatx-voicepack-"));
process.env.LEADS_DIR = TMP;
process.env.CHATX_VOICEPACK_DIR = path.join(TMP, "pack");
process.env.CHATX_USER_VOICE_DIR = path.join(TMP, "users");
const DEF = path.join(TMP, "default.mp3");
fs.writeFileSync(DEF, "DEFAULT");
process.env.CHATX_BOT_VOICE_REF = DEF;
delete process.env.CHATX_BOT_VOICE_REF_LOVER;
delete process.env.CHATX_BOT_VOICE_REF_SALES;

function tone(sr: number, sec: number, amp = 0.3, silentSec = 0): Float32Array {
  const n = Math.round(sr * (sec + silentSec));
  const out = new Float32Array(n);
  const voiced = Math.round(sr * sec);
  for (let i = 0; i < voiced; i++) out[i] = Math.sin((2 * Math.PI * 220 * i) / sr) * amp * (0.6 + 0.4 * Math.sin(i / 900));
  return out;
}

async function main() {
  const R = await import("./chatx-voice-ref");
  const P = await import("./chatx-voicepack");
  const V = await import("./chatx-voice");
  const C = await import("./chatx-voice-clone");

  // ① 体检阈值
  const sr = 24000;
  assert.strictEqual(R.analyzeReference(tone(sr, 10), sr).grade, "green", "10s 清晰 → 绿");
  const short = R.analyzeReference(tone(sr, 2), sr);
  assert.strictEqual(short.grade, "red");
  assert.ok(short.issues.includes("录音过短"));
  assert.strictEqual(R.analyzeReference(tone(sr, 4), sr).grade, "yellow", "3–5s 黄");
  const clip = new Float32Array(tone(sr, 8, 1.5)).map((x) => Math.max(-1, Math.min(1, x)));
  const ch = R.analyzeReference(clip, sr);
  assert.strictEqual(ch.grade, "red");
  assert.ok(ch.issues.includes("削波破音"));
  const sil = R.analyzeReference(tone(sr, 3, 0.3, 9), sr);
  assert.ok(sil.issues.includes("有效人声过少"), JSON.stringify(sil));
  assert.strictEqual(R.analyzeReference(new Float32Array(sr * 8), sr).grade, "red", "全静音红");
  const wav = R.encodeWavMono(tone(sr, 1), sr);
  const back = R.decodeWavMono(wav)!;
  assert.strictEqual(back.sampleRate, sr);
  assert.strictEqual(back.samples.length, sr);
  assert.ok(R.curateReference(tone(sr, 20), sr).length <= 15 * sr + 1, "策展裁到 15s");

  // ② 选择解析
  assert.strictEqual(P.parseVoiceChoice("SSB0016"), "SSB0016");
  assert.strictEqual(P.parseVoiceChoice("../../etc/passwd"), null, "非库 id 拒绝");
  assert.strictEqual(P.parseVoiceChoice("SSB9999"), null);
  assert.deepStrictEqual(P.resolveVoiceChoice({}, "xiaojie"), { choice: "default", explicit: false }, "小界默认不变");
  assert.deepStrictEqual(P.resolveVoiceChoice({}, "lover"), { choice: "default", explicit: false }, "没选过 → 行为不变");
  assert.strictEqual(P.PERSONA_DEFAULT_VOICE.lover, "SSB0016");
  assert.deepStrictEqual(P.resolveVoiceChoice({ voices: { lover: "SSB0966" } }, "lover"), { choice: "SSB0966", explicit: true });
  assert.deepStrictEqual(P.resolveVoiceChoice({ voices: { lover: "mine" } }, "lover").choice, "default", "没有克隆音时 mine 无效");

  // ③ referenceAudio 回落链
  const b64 = (s: string) => Buffer.from(s).toString("base64");
  let r = await V.referenceAudio("lover", {});
  assert.strictEqual(r!.ref, "default", "库文件没抓 → 默认参考音（语音永远有声）");
  fs.mkdirSync(P.VOICEPACK_DIR, { recursive: true });
  fs.writeFileSync(P.voicepackPath("SSB0016"), "LOVERLIB");
  fs.writeFileSync(P.voicepackPath("SSB0966"), "STILL");
  r = await V.referenceAudio("lover", {});
  assert.strictEqual(r!.ref, "default", "库文件在也不自动套推荐音色");
  r = await V.referenceAudio("lover", { voices: { lover: "SSB0016" } });
  assert.deepStrictEqual([r!.ref, r!.voice, r!.b64], ["lib", "SSB0016", b64("LOVERLIB")], "选库音色");
  const envRef = path.join(TMP, "lover-env.wav");
  fs.writeFileSync(envRef, "ENV");
  process.env.CHATX_BOT_VOICE_REF_LOVER = envRef;
  r = await V.referenceAudio("lover", {});
  assert.strictEqual(r!.ref, "persona", "没选过 → 运维环境变量（同旧行为）");
  r = await V.referenceAudio("lover", { voices: { lover: "SSB0966" } });
  assert.deepStrictEqual([r!.ref, r!.voice], ["lib", "SSB0966"], "用户显式选择优先于环境变量");
  r = await V.referenceAudio("lover", { voices: { lover: "default" } });
  assert.strictEqual(r!.ref, "default", "显式恢复默认");
  delete process.env.CHATX_BOT_VOICE_REF_LOVER;
  r = await V.referenceAudio("sales", { voices: { sales: "SSB1136" } });
  assert.strictEqual(r!.ref, "default", "选中的库文件缺失 → 回落");
  r = await V.referenceAudio("xiaojie");
  assert.strictEqual(r!.ref, "default", "旧调用方式不变");

  // ④ 克隆登记
  assert.ok(C.cloneArmed(new Date().toISOString()));
  assert.ok(!C.cloneArmed(new Date(Date.now() - 11 * 60_000).toISOString()), "10 分钟过期");
  assert.ok(!C.cloneArmed(undefined));
  const bad = await C.enrollUserVoice(42, Buffer.from("not audio"), "t");
  assert.deepStrictEqual(bad, { ok: false, code: "not_decodable" });
  const tooShort = await C.enrollUserVoice(42, R.encodeWavMono(tone(48000, 2), 48000), "t");
  assert.ok(!tooShort.ok && tooShort.code === "rejected" && tooShort.health!.issues.includes("录音过短"));
  assert.match(C.cloneRejectText(tooShort as never, "zh"), /录音过短/);
  const good = await C.enrollUserVoice(42, R.encodeWavMono(tone(48000, 9), 48000), "2026-09-24T00:00:00Z");
  assert.ok(good.ok);
  if (!good.ok) return;
  assert.strictEqual(good.voice.owner_consent, true);
  assert.strictEqual(path.dirname(good.voice.path), path.resolve(P.USER_VOICE_DIR));
  const saved = R.decodeWavMono(fs.readFileSync(good.voice.path))!;
  assert.strictEqual(saved.sampleRate, 24000, "48k → 24k");
  assert.ok(Math.abs(good.voice.sec - 9) < 0.6, String(good.voice.sec));
  const mv = { myVoice: good.voice, voices: { sales: "mine" } };
  r = await V.referenceAudio("sales", mv);
  assert.deepStrictEqual([r!.ref, r!.voice], ["mine", "mine"], "克隆音生效");
  r = await V.referenceAudio("xiaojie", mv);
  assert.strictEqual(r!.ref, "default", "克隆音不会顶掉没选它的人设");

  // ⑤ 删除只删私有目录
  const outside = path.join(TMP, "keep.wav");
  fs.writeFileSync(outside, "x");
  await C.removeUserVoiceFile(outside);
  assert.ok(fs.existsSync(outside), "目录外不删");
  await C.removeUserVoiceFile(good.voice.path);
  assert.ok(!fs.existsSync(good.voice.path));
  r = await V.referenceAudio("sales", mv);
  assert.strictEqual(r!.ref, "default", "克隆文件没了 → 回落");

  const avail = await P.availableVoices();
  assert.deepStrictEqual([...avail].sort(), ["SSB0016", "SSB0966"]);
  console.log("chatx-voicepack OK");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
