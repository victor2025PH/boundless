/**
 * PTT 格式魔数门禁（node --test，零新依赖）。
 * 锁死：WAV/MP3/假 .ogg 不得被判为可发 voice。
 */
import test from "node:test";
import assert from "node:assert/strict";
import { looksLikeOggOpus } from "../ptt-format.js";

function minimalOggOpus() {
  // 与 Python voice_ptt_gate 同口径：OggS + OpusHead 落在前 512 字节
  return Buffer.concat([
    Buffer.from("OggS"),
    Buffer.alloc(60),
    Buffer.from("OpusHead"),
    Buffer.alloc(32),
  ]);
}

test("合法 OggS+OpusHead → true", () => {
  assert.equal(looksLikeOggOpus(minimalOggOpus()), true);
});

test("WAV 头（RIFF）→ false（事故原形）", () => {
  const wav = Buffer.concat([
    Buffer.from("RIFF"),
    Buffer.alloc(4),
    Buffer.from("WAVE"),
    Buffer.alloc(80),
  ]);
  assert.equal(looksLikeOggOpus(wav), false);
});

test("MP3 帧同步字 → false", () => {
  const mp3 = Buffer.concat([Buffer.from([0xff, 0xfb]), Buffer.alloc(80)]);
  assert.equal(looksLikeOggOpus(mp3), false);
});

test("假 .ogg：只有 OggS 无 OpusHead → false", () => {
  const fake = Buffer.concat([Buffer.from("OggS"), Buffer.alloc(80, 0x41)]);
  assert.equal(looksLikeOggOpus(fake), false);
});

test("空/过短/非 buffer → false", () => {
  assert.equal(looksLikeOggOpus(null), false);
  assert.equal(looksLikeOggOpus(Buffer.alloc(0)), false);
  assert.equal(looksLikeOggOpus(Buffer.from("OggS")), false);
});
