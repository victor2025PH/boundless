// 集团账本全局 ID：`<prefix>_<ULID>`。
// ULID = 26 字符 Crockford Base32 大写（字母表 0123456789ABCDEFGHJKMNPQRSTVWXYZ，
// 排除 I/L/O/U），48bit 毫秒时间戳 + 80bit 随机（node:crypto randomBytes，无第三方依赖）。
// 时间戳在前 → ID 字典序 ≈ 创建时间序（同毫秒内不保证单调）。
// 前缀约定：cust=客户 ord=订单 lic=授权 evt=事件 aud=审计 usr=控制台用户 prs=人设 opl=商机跟进。
// 旧订单号（AH-YYYYMMDD-XXXXXX）与留资 key（tg:xxx / c:xxx）不走此规范，保留为 source_key 自然键。
import { randomBytes } from "node:crypto";

const ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

export const ID_PREFIXES = ["cust", "ord", "lic", "evt", "aud", "usr", "prs", "opl"] as const;
export type IdPrefix = (typeof ID_PREFIXES)[number];

/** 完整 ID 校验：2–5 位小写前缀 + "_" + 26 位 Crockford Base32。 */
export const ID_REGEX = /^[a-z]{2,5}_[0-9A-HJKMNP-TV-Z]{26}$/;

const TIME_LEN = 10; // 48bit → 10 字符（最高 2bit 恒为 0）
const MAX_TIME = 2 ** 48 - 1;

function encodeTime(ms: number): string {
  let t = Math.floor(ms);
  if (!Number.isFinite(t) || t < 0) t = 0;
  if (t > MAX_TIME) t = MAX_TIME;
  const out = new Array<string>(TIME_LEN);
  // 48bit 超出 32 位位运算范围，用 Math.floor 除法（< 2^53 精确）
  for (let i = TIME_LEN - 1; i >= 0; i--) {
    out[i] = ALPHABET[t % 32];
    t = Math.floor(t / 32);
  }
  return out.join("");
}

function encodeRandom(bytes: Uint8Array): string {
  // 10 字节 = 80bit = 16 个 5bit 组，恰好无补位
  let out = "";
  let acc = 0;
  let bits = 0;
  for (const b of bytes) {
    acc = (acc << 8) | b;
    bits += 8;
    while (bits >= 5) {
      out += ALPHABET[(acc >>> (bits - 5)) & 31];
      bits -= 5;
    }
    acc &= (1 << bits) - 1; // 只留未消费的低位，防 32bit 溢出
  }
  return out;
}

/** 裸 ULID（无前缀）。可注入时间戳（默认 Date.now()），便于测试。 */
export function ulid(time = Date.now()): string {
  return encodeTime(time) + encodeRandom(randomBytes(10));
}

/** 生成带前缀 ID：newId("ord") → "ord_01JZ…"。前缀必须为 2–5 位小写字母，否则抛错。 */
export function newId(prefix: IdPrefix | (string & {})): string {
  if (!/^[a-z]{2,5}$/.test(prefix)) throw new TypeError(`invalid id prefix: ${prefix}`);
  return `${prefix}_${ulid()}`;
}

/** 校验 ID 是否符合 `<prefix>_<ULID>` 规范；传 prefix 时同时要求前缀精确匹配。 */
export function isValidId(id: unknown, prefix?: string): boolean {
  if (typeof id !== "string" || !ID_REGEX.test(id)) return false;
  return prefix ? id.startsWith(prefix + "_") : true;
}
