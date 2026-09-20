/**
 * WhatsApp PTT（语音条）格式硬闸——零依赖纯函数。
 *
 * Baileys send-media 对 media_type=voice 硬编码
 * ``mimetype: audio/ogg; codecs=opus`` + ``ptt: true``。上游若把 WAV/MP3
 * 当 voice 发出，服务端建得了气泡，客户手机解码失败弹「无法下载音频」
 * （2026-08-04 智拓桌面机事故）。本模块在边车发前用魔数二次把关。
 *
 * 为何独立文件：同 close-policy —— server.js 顶层 app.listen，测试不可安全
 * import；纯函数可被 node --test 直接锁死。
 */

/**
 * @param {Buffer|Uint8Array|null|undefined} buf
 * @returns {boolean}
 */
export function looksLikeOggOpus(buf) {
  if (!buf || typeof buf.length !== "number" || buf.length < 64) return false;
  const head4 = Buffer.isBuffer(buf)
    ? buf.subarray(0, 4).toString("ascii")
    : Buffer.from(buf.subarray(0, 4)).toString("ascii");
  if (head4 !== "OggS") return false;
  const n = Math.min(buf.length, 512);
  const window = Buffer.isBuffer(buf) ? buf.subarray(0, n) : Buffer.from(buf.subarray(0, n));
  return window.includes(Buffer.from("OpusHead"));
}
