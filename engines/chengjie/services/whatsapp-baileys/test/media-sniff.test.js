/**
 * send-media 缺类型回退嗅探契约（工单 #143）：
 *   - JPEG/PNG/WebP → image；OGG → audio；认不出/太短 → ""
 *   - 白名单集合锁死——"photo" 等别名必须不在其中（不在 → 触发嗅探回退）
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { KNOWN_MEDIA_TYPES, sniffMediaKind } from "../media-sniff.js";

const pad = (head) => Buffer.concat([head, Buffer.alloc(16)]);

test("JPEG/PNG/WebP magic → image", () => {
  assert.equal(sniffMediaKind(pad(Buffer.from([0xff, 0xd8, 0xff, 0xe0]))), "image");
  assert.equal(
    sniffMediaKind(pad(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))),
    "image",
  );
  const webp = Buffer.concat([
    Buffer.from("RIFF"), Buffer.alloc(4), Buffer.from("WEBP"), Buffer.alloc(8),
  ]);
  assert.equal(sniffMediaKind(webp), "image");
});

test("OggS magic → audio（非 ptt；voice 语义仍只走显式 media_type=voice）", () => {
  assert.equal(sniffMediaKind(pad(Buffer.from("OggS"))), "audio");
});

test("认不出/太短/空 → 空串（调用方落 document）", () => {
  assert.equal(sniffMediaKind(pad(Buffer.from("%PDF"))), "");
  assert.equal(sniffMediaKind(Buffer.from([0xff, 0xd8])), ""); // <12 字节
  assert.equal(sniffMediaKind(null), "");
  assert.equal(sniffMediaKind(undefined), "");
});

test("白名单锁死：photo 等别名必须不在其中（触发嗅探回退）", () => {
  for (const t of ["image", "voice", "video", "sticker", "document", "file"]) {
    assert.ok(KNOWN_MEDIA_TYPES.has(t), `${t} 应在白名单`);
  }
  for (const t of ["photo", "img", "audio", "gif", ""]) {
    assert.ok(!KNOWN_MEDIA_TYPES.has(t), `${t || "(empty)"} 不应在白名单`);
  }
});
