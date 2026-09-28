/**
 * 定时帖配图的本地存储：控制台直接上传图片 → DATA_DIR/tg_post_media/<随机名>.<ext>，帖子里记 `media:<文件名>`，
 * 发送时按 multipart 上传给 Telegram（不依赖公网图片地址）。只收 JPG / PNG / WebP，按文件头判断类型，≤ 5MB。
 */
import { randomBytes } from "crypto";
import { mkdir, readFile, readdir, stat, unlink, writeFile } from "fs/promises";
import path from "path";
import { DATA_DIR } from "./data-dir";

const DIR = process.env.TG_POST_MEDIA_DIR || path.join(DATA_DIR, "tg_post_media");
export const MEDIA_MAX_BYTES = 5 * 1024 * 1024;
const REF_RE = /^media:([a-f0-9]{24}\.(jpg|png|webp))$/;
const NAME_RE = /^[a-f0-9]{24}\.(jpg|png|webp)$/;
/** 上传后至少保留这么久才可能被清理：刚上传还没点「排期」的图不会被误删 */
export const MEDIA_MIN_AGE_MS = 24 * 3600_000;
const TYPES = { jpg: "image/jpeg", png: "image/png", webp: "image/webp" } as const;
type Ext = keyof typeof TYPES;

export class MediaError extends Error {}

function sniff(buf: Buffer): Ext | null {
  if (buf.length > 3 && buf[0] === 0xff && buf[1] === 0xd8 && buf[2] === 0xff) return "jpg";
  if (buf.length > 8 && buf.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) return "png";
  if (buf.length > 12 && buf.toString("ascii", 0, 4) === "RIFF" && buf.toString("ascii", 8, 12) === "WEBP") return "webp";
  return null;
}

export function isMediaRef(v: string): boolean {
  return REF_RE.test(v);
}

export async function saveMedia(buf: Buffer): Promise<string> {
  if (!buf.length) throw new MediaError("没有收到图片");
  if (buf.length > MEDIA_MAX_BYTES) throw new MediaError("图片最大 5MB");
  const ext = sniff(buf);
  if (!ext) throw new MediaError("只支持 JPG / PNG / WebP 图片");
  const name = `${randomBytes(12).toString("hex")}.${ext}`;
  await mkdir(DIR, { recursive: true });
  await writeFile(path.join(DIR, name), buf);
  return `media:${name}`;
}

/**
 * 清理没被任何帖子引用、且上传超过 minAge 的图片。只动本目录下符合上传命名的文件，其他一律不碰；
 * 删失败不抛，下次巡检再试。referenced 传 `media:<文件名>` 或纯文件名都行。
 */
export async function pruneUnusedMedia(referenced: Iterable<string>, now = Date.now(), minAgeMs = MEDIA_MIN_AGE_MS): Promise<{ scanned: number; removed: string[]; kept: number }> {
  const keep = new Set<string>();
  for (const r of referenced) keep.add(r.startsWith("media:") ? r.slice(6) : r);
  let names: string[] = [];
  try {
    names = (await readdir(DIR)).filter((n) => NAME_RE.test(n));
  } catch {
    return { scanned: 0, removed: [], kept: 0 };
  }
  const removed: string[] = [];
  for (const n of names) {
    if (keep.has(n)) continue;
    try {
      const st = await stat(path.join(DIR, n));
      if (now - st.mtimeMs < minAgeMs) continue;
      await unlink(path.join(DIR, n));
      removed.push(n);
    } catch {
      /* 并发被删 / 权限问题：下次再试 */
    }
  }
  return { scanned: names.length, removed, kept: names.length - removed.length };
}

export async function readMedia(ref: string): Promise<{ name: string; data: Buffer; type: string } | null> {
  const m = REF_RE.exec(ref);
  if (!m) return null;
  try {
    return { name: m[1], data: await readFile(path.join(DIR, m[1])), type: TYPES[m[2] as Ext] };
  } catch {
    return null;
  }
}
