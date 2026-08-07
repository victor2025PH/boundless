import { mkdir, readFile, rename, writeFile } from "fs/promises";
import path from "path";
import { DATA_DIR } from "./data-dir";

// 厂商已签名吊销名单（CRL）的服务器侧存取：文件即真相，只经 /api/revocations 的
// requireAdmin POST 写入（厂商机 push_revocations.py 推送）。Ed25519 签名由客户端
// 内置公钥验证——服务器不持私钥、不验签也伪造不了有效名单（客户端 fail-safe）。
const FILE = process.env.LICENSE_CRL_FILE || path.join(DATA_DIR, "revocations.json");

export type CrlDoc = {
  payload: { v?: number; updated?: number; revoked: Array<Record<string, unknown>> };
  sig: string;
};

export async function readCrlDoc(): Promise<CrlDoc | null> {
  try {
    const doc = JSON.parse(await readFile(FILE, "utf-8"));
    if (doc?.payload && Array.isArray(doc.payload.revoked)) return doc as CrlDoc;
  } catch {
    /* 文件不存在/损坏 = 无吊销（与客户端 fail-safe 同语义） */
  }
  return null;
}

export async function writeCrlDoc(doc: object) {
  await mkdir(path.dirname(FILE), { recursive: true });
  const tmp = FILE + ".tmp";
  await writeFile(tmp, JSON.stringify(doc));
  await rename(tmp, FILE);
}

// 与客户端 license.py _match_revoke_entry 同语义：条目内 AND（列出的标识键都要相等）、
// 名单内 OR（任一条目命中即吊销）、空条目不匹配（防「误配置的空条目吊销一切」）。
const MATCH_KEYS = ["lic_id", "machine", "licensee", "issued"] as const;

export function isRevokedPayload(payload: Record<string, unknown>, crl: CrlDoc | null): boolean {
  if (!crl) return false;
  for (const entry of crl.payload.revoked) {
    if (!entry || typeof entry !== "object") continue;
    const keys = MATCH_KEYS.filter((k) => entry[k] !== undefined && entry[k] !== null && entry[k] !== "");
    if (!keys.length) continue;
    if (keys.every((k) => String((payload as Record<string, unknown>)?.[k] ?? "") === String(entry[k]))) return true;
  }
  return false;
}
