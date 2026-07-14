import { NextRequest, NextResponse } from "next/server";
import { appendFile, mkdir, readdir, stat, unlink, writeFile } from "fs/promises";
import path from "path";
import { DATA_DIR } from "@/lib/data-dir";
import { notifyAdmins } from "@/lib/order-store";
import { requireAdmin } from "@/lib/admin-auth";
import { getAdminChats } from "@/lib/admin-store";
import { sendDocument } from "@/lib/telegram-bot";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// 客户端「一键诊断包直传」：POST 原始 zip（application/zip）→ 存盘 → 返回 6 位短码。
// 客户只要把短码报给客服；客服凭码取包（GET 带管理密钥），不再折腾「找文件-发文件」。
// 体量防线：≤15MB（nginx 站点级 20m 之内）；保留 30 天自动清理；管理员 TG 即时知会。

const DIAG_DIR = path.join(DATA_DIR, "diag");
const MAX_BYTES = 15 * 1024 * 1024;
const KEEP_DAYS = 30;
// 无易混字符（0O1IL）的短码字母表：口头/聊天报码不出错
const ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ";

function newCode(): string {
  let s = "";
  for (let i = 0; i < 6; i++) s += ALPHABET[Math.floor(Math.random() * ALPHABET.length)];
  return s;
}

async function cleanup() {
  try {
    const now = Date.now();
    for (const f of await readdir(DIAG_DIR)) {
      const p = path.join(DIAG_DIR, f);
      const st = await stat(p).catch(() => null);
      if (st && now - st.mtimeMs > KEEP_DAYS * 86400_000) await unlink(p).catch(() => {});
    }
  } catch {
    /* dir may not exist yet */
  }
}

export async function POST(req: NextRequest) {
  const len = Number(req.headers.get("content-length") || 0);
  if (len > MAX_BYTES) {
    return NextResponse.json({ ok: false, error: "包体超过 15MB 上限" }, { status: 413 });
  }
  let buf: Buffer;
  try {
    buf = Buffer.from(await req.arrayBuffer());
  } catch {
    return NextResponse.json({ ok: false, error: "bad_body" }, { status: 400 });
  }
  // zip 魔数校验（PK\x03\x04）：这里只收诊断 zip，不做通用文件床
  if (buf.length < 100 || buf.length > MAX_BYTES || buf[0] !== 0x50 || buf[1] !== 0x4b) {
    return NextResponse.json({ ok: false, error: "仅接受诊断包 zip" }, { status: 400 });
  }
  let meta: Record<string, unknown> = {};
  try {
    meta = JSON.parse(req.headers.get("x-diag-meta") || "{}");
  } catch {
    /* meta 可缺省 */
  }
  await mkdir(DIAG_DIR, { recursive: true });
  await cleanup();
  const code = newCode();
  await writeFile(path.join(DIAG_DIR, `${code}.zip`), buf);
  const rec = {
    t: new Date().toISOString(),
    code,
    bytes: buf.length,
    app: String(meta?.app ?? "").slice(0, 24),
    fp: String(meta?.fp ?? "").slice(0, 24),
    ip: (req.headers.get("x-forwarded-for")?.split(",")[0] || "").trim().slice(0, 60),
  };
  await writeFile(path.join(DIAG_DIR, `${code}.json`), JSON.stringify(rec));
  await appendFile(path.join(DIAG_DIR, "uploads.jsonl"), JSON.stringify(rec) + "\n").catch(() => {});
  // 诊断包直接送进客服 TG（≤15MB 远在 Bot API 50MB 上限内）——客服零步取包；
  // 发送失败（网络/未配 bot）退回短码通知，仍可 /diag 码 或 API 补取。
  const caption =
    `🧰 收到诊断包 ${code}（${(buf.length / 1048576).toFixed(1)}MB` +
    (rec.app ? ` · v${rec.app}` : "") + (rec.fp ? ` · 指纹 ${rec.fp}…` : "") + "）";
  let pushed = 0;
  try {
    const chats = await getAdminChats();
    for (const chat of chats) {
      const r = await sendDocument(chat, `diag-${code}.zip`, buf, caption);
      if (r?.ok) pushed++;
    }
  } catch {
    /* 推送失败走兜底文本 */
  }
  if (pushed === 0) {
    await notifyAdmins(caption + `\n取包：回复 <code>/diag ${code}</code>，或 /api/diag-upload?code=${code}&key=…`).catch(() => {});
  }
  return NextResponse.json({ ok: true, code });
}

/** 客服取包：GET /api/diag-upload?code=XXXXXX（requireAdmin：cookie / x-setup-key / ?key=）。 */
export async function GET(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const code = (req.nextUrl.searchParams.get("code") || "").trim().toUpperCase();
  if (!/^[A-Z2-9]{6}$/.test(code)) {
    return NextResponse.json({ ok: false, error: "bad_code" }, { status: 400 });
  }
  try {
    const p = path.join(DIAG_DIR, `${code}.zip`);
    const { readFile } = await import("fs/promises");
    const buf = await readFile(p);
    return new NextResponse(new Uint8Array(buf), {
      headers: {
        "Content-Type": "application/zip",
        "Content-Disposition": `attachment; filename="diag-${code}.zip"`,
      },
    });
  } catch {
    return NextResponse.json({ ok: false, error: "not_found" }, { status: 404 });
  }
}
