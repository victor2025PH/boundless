#!/usr/bin/env node
/**
 * 断言：purged 墓碑 scrub 后 slots_detail 不含 fingerprint/ref。
 * 算法与 website/lib/personas.ts → scrubSlotsDetailFingerprints 保持同步。
 *
 *   node scripts/assert-persona-purged-scrub.mjs
 */
const SLOTS = ["face", "voice", "prompt", "knowledge"];

function scrubSlotsDetailFingerprints(slotsDetail) {
  if (slotsDetail == null || slotsDetail === "") return slotsDetail;
  try {
    const obj = JSON.parse(slotsDetail);
    if (!obj || typeof obj !== "object" || Array.isArray(obj)) return null;
    for (const key of SLOTS) {
      const slot = obj[key];
      if (slot && typeof slot === "object" && !Array.isArray(slot)) {
        delete slot.fingerprint;
        delete slot.ref;
      }
    }
    return JSON.stringify(obj);
  } catch {
    return null;
  }
}

function hasFpOrRef(raw) {
  if (!raw) return false;
  try {
    const obj = JSON.parse(raw);
    return SLOTS.some((k) => {
      const s = obj?.[k];
      return s && (s.fingerprint != null || s.ref != null);
    });
  } catch {
    return /fingerprint|"ref"\s*:/.test(String(raw));
  }
}

const input = JSON.stringify({
  face: { fingerprint: "abc123deadbeef", ref: "voices/x.wav", version: 1 },
  voice: { fingerprint: "ffff", ref: "db#row" },
  prompt: { version: 2 },
  _meta: { customer_name: "审计可留" },
});

const out = scrubSlotsDetailFingerprints(input);
const parsed = JSON.parse(out);

const checks = [
  ["去掉 face.fingerprint", parsed.face.fingerprint === undefined],
  ["去掉 face.ref", parsed.face.ref === undefined],
  ["保留 face.version", parsed.face.version === 1],
  ["去掉 voice.fingerprint/ref", parsed.voice.fingerprint === undefined && parsed.voice.ref === undefined],
  ["保留 prompt.version", parsed.prompt.version === 2],
  ["保留 _meta", parsed._meta?.customer_name === "审计可留"],
  ["hasFpOrRef(out)===false", hasFpOrRef(out) === false],
  ["空串原样", scrubSlotsDetailFingerprints("") === ""],
  ["null 原样", scrubSlotsDetailFingerprints(null) === null],
  ["坏 JSON → null", scrubSlotsDetailFingerprints("{not-json") === null],
];

let failed = 0;
for (const [desc, ok] of checks) {
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${desc}`);
  if (!ok) failed += 1;
}

if (failed) {
  console.error(`== assert-persona-purged-scrub: ${failed} 项失败 ==`);
  process.exit(1);
}
console.log("== assert-persona-purged-scrub: 全部通过（purged 行无 fingerprint/ref）==");
process.exit(0);
