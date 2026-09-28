/**
 * 音色选择 / 克隆漏斗聚合：npx tsx lib/chatx-report-voices.test.ts
 */
import assert from "assert";
import { buildDailyReport, buildFeatureInsights, formatDailyDigest, type Ev } from "./chatx-report";

const D1 = "2026-09-22";
const at = (hm: string) => new Date(`${D1}T${hm}:00+08:00`).toISOString();
const ev = (t: string, event: string, props: Record<string, unknown>): Ev => ({ t, event, props });

const events: Ev[] = [
  ev(at("09:00"), "chatx_bot_start", { src: "ad_a", uid: 1, first: true }),
  ev(at("09:00"), "chatx_bot_start", { src: "ad_b", uid: 2, first: true }),
  ev(at("09:01"), "chatx_bot_voice_pick", { src: "ad_a", uid: 1, persona: "lover", voice: "SSB0016", preview: true }),
  ev(at("09:02"), "chatx_bot_voice_pick", { src: "ad_a", uid: 1, persona: "sales", voice: "SSB0016", preview: true }),
  ev(at("09:03"), "chatx_bot_voice_pick", { src: "ad_b", uid: 2, persona: "sales", voice: "mine", preview: false }),
  ev(at("09:04"), "chatx_bot_voice_out", { src: "ad_a", uid: 1, ok: true, sec: 5, ref: "lib", voice: "SSB0016" }),
  ev(at("09:05"), "chatx_bot_voice_out", { src: "ad_a", uid: 1, ok: true, sec: 5, ref: "default" }),
  ev(at("09:06"), "chatx_bot_voice_out", { src: "ad_a", uid: 1, ok: false, why: "tts_failed", voice: "SSB0016" }),
  ev(at("09:10"), "chatx_bot_voice_clone", { src: "ad_b", uid: 2, step: "start" }),
  ev(at("09:11"), "chatx_bot_voice_clone", { src: "ad_b", uid: 2, step: "consent" }),
  ev(at("09:12"), "chatx_bot_voice_clone", { src: "ad_b", uid: 2, step: "sample", ok: false, why: "rejected", issue: "录音过短" }),
  ev(at("09:13"), "chatx_bot_voice_clone", { src: "ad_b", uid: 2, step: "sample", ok: false, why: "not_decodable" }),
  ev(at("09:14"), "chatx_bot_voice_clone", { uid: 2, step: "sample", ok: true, grade: "green" }),
  ev(at("09:15"), "chatx_bot_voice_clone", { src: "ad_b", uid: 2, step: "delete" }),
  ev(at("09:16"), "chatx_bot_voice_clone", { src: "ad_a", uid: 1, step: "start" }),
];

const r = buildDailyReport(events, [D1], Date.parse(at("23:00")));
const f = r.features[0];
assert.deepStrictEqual(f.voicePicks, { SSB0016: 2, mine: 1 });
assert.deepStrictEqual(f.voiceOutByVoice, { SSB0016: 1, default: 1 }, "只算成功的，缺 voice 看 ref");
assert.strictEqual(f.cloneStart, 2);
assert.strictEqual(f.cloneConsent, 1);
assert.strictEqual(f.cloneOk, 1);
assert.strictEqual(f.cloneDel, 1);
assert.deepStrictEqual(f.cloneFail, { 录音过短: 1, not_decodable: 1 }, "体检拒收按具体问题分");
assert.deepStrictEqual(f.cloneOkBySrc, { ad_b: 1 }, "无 src 的事件按 uid 归来源");

const digest = formatDailyDigest(r, D1);
assert.match(digest, /🎧 音色：选择 3 次（SSB0016 2 \/ 我的声音 1） · 克隆 2 → 同意 1 → 成功 1（ad_b 1）/);

// 样本 ≥5 才下结论；同意率低 → 提示授权文案
const base = { ...f, cloneStart: 10, cloneConsent: 2, cloneOk: 1, cloneFail: {} };
assert.ok(buildFeatureInsights([base]).some((i) => i.level === "warn" && /同意授权/.test(i.text)));
const rej = { ...f, cloneStart: 10, cloneConsent: 9, cloneOk: 1, cloneFail: { 录音过短: 5, 削波破音: 1 } };
assert.ok(buildFeatureInsights([rej]).some((i) => i.level === "warn" && /录音过短/.test(i.text)));
const good = { ...f, cloneStart: 10, cloneConsent: 9, cloneOk: 8, cloneFail: { 录音过短: 1 } };
assert.ok(buildFeatureInsights([good]).some((i) => i.level === "info" && /成功 8（80%）/.test(i.text)));
assert.ok(!buildFeatureInsights([f]).some((i) => /克隆/.test(i.text)), "小样本不下结论");

console.log("chatx-report-voices OK");
