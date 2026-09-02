/**
 * send-media 缺类型回退的文件魔数嗅探——零依赖纯函数（工单 #143，2026-09-02）。
 *
 * 背景：send-media 的 media_type 曾缺省 document——调用方漏传/传了陌生值
 * （相册库的 "photo"）时，一张 JPEG 会按 document 发出，对方端显示成
 * 点不开的「文档」（skuio 客户实锤 "I can't see this What is it?"）。
 * 本模块按文件头兜底：JPEG/PNG/WebP→image，OGG→audio；认不出返回 ""
 * （调用方落 document 并打 WARNING）。
 *
 * 为何独立文件：同 ptt-format —— server.js 顶层 app.listen，测试不可安全
 * import；纯函数可被 node --test 直接锁死。
 */

/** send-media 认识的 media_type 白名单；此外的值（含缺失）走魔数嗅探回退。 */
export const KNOWN_MEDIA_TYPES = new Set([
  "image", "voice", "video", "sticker", "document", "file",
]);

/**
 * @param {Buffer|Uint8Array|null|undefined} buf
 * @returns {"image"|"audio"|""}
 */
export function sniffMediaKind(buf) {
  if (!buf || typeof buf.length !== "number" || buf.length < 12) return "";
  const b = Buffer.isBuffer(buf) ? buf : Buffer.from(buf.subarray(0, 12));
  if (b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return "image"; // JPEG
  if (b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47) {
    return "image"; // PNG
  }
  if (
    b.subarray(0, 4).toString("latin1") === "RIFF" &&
    b.subarray(8, 12).toString("latin1") === "WEBP"
  ) {
    return "image"; // WebP
  }
  if (b.subarray(0, 4).toString("latin1") === "OggS") return "audio";
  return "";
}
