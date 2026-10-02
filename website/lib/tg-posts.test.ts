/**
 * 定时发帖：npx tsx lib/tg-posts.test.ts（mock fetch，不打 Telegram）。
 * 断言：校验（客服群 / 停用 / 非管理员 / 过期时间 / 按钮）；到点才发；只发一次；失败记原因；取消。
 */
import assert from "assert";
import fs from "fs";
import os from "os";
import path from "path";

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "tg-posts-"));
process.env.DATA_DIR = TMP;
process.env.TG_HUB_FILE = path.join(TMP, "tg_hub.json");
process.env.TG_POSTS_FILE = path.join(TMP, "tg_posts.json");
process.env.CHATX_BOT_TOKEN = "123:TEST";
process.env.TG_POST_MEDIA_DIR = path.join(TMP, "media");

type Call = { method: string; body: Record<string, unknown> };
let calls: Call[] = [];
let failNext = false;
globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
  const url = String(input);
  const method = url.split("/").pop() || "";
  const body = init && typeof init.body === "string" ? (JSON.parse(init.body) as Record<string, unknown>) : {};
  calls.push({ method, body });
  if (failNext) {
    failNext = false;
    return new Response(JSON.stringify({ ok: false, description: "Bad Request: chat not found" }), { status: 400 });
  }
  return new Response(JSON.stringify({ ok: true, result: { message_id: 555 } }), { status: 200 });
}) as typeof fetch;

async function main() {
  const H = await import("./tg-hub-store");
  const P = await import("./tg-posts");
  const base = { type: "channel", features: H.DEFAULT_FEATURES, discoveredAt: "", updatedAt: "", botId: "chatx" };
  await H.mutateHub((hub) => {
    hub.chats = [
      { ...base, chatId: "-1001", title: "频道", role: "channel", enabled: true, botStatus: "administrator" },
      { ...base, chatId: "-1002", title: "客服", role: "support", enabled: true, botStatus: "administrator", type: "supergroup" },
      { ...base, chatId: "-1003", title: "停用", role: "channel", enabled: false, botStatus: "administrator" },
      { ...base, chatId: "-1004", title: "非管理员", role: "community", enabled: true, botStatus: "member", type: "supergroup" },
    ];
  });
  const now = Date.parse("2026-09-25T10:00:00Z");
  const mk = (o: Partial<Parameters<typeof P.schedulePost>[0]>) => P.schedulePost({ botId: "chatx", chatId: "-1001", text: "hello", createdBy: "t", ...o }, now);

  await assert.rejects(mk({ chatId: "-1002" }), /客服群/);
  await assert.rejects(mk({ chatId: "-1003" }), /停用/);
  await assert.rejects(mk({ chatId: "-1004" }), /管理员/);
  await assert.rejects(mk({ chatId: "-9" }), /没有/);
  await assert.rejects(mk({ text: "  " }), /内容/);
  await assert.rejects(mk({ sendAt: "2026-09-25T09:00:00Z" }), /过了/);
  await assert.rejects(mk({ buttonText: "go" }), /一起填/);
  await assert.rejects(mk({ buttonText: "go", buttonUrl: "javascript:alert(1)" }), /https/);

  const later = await mk({ sendAt: "2026-09-25T12:00:00Z", text: "定时帖", buttonText: "打开 bot", buttonUrl: "https://t.me/ctx2026_bot?start=ad_x" });
  const asap = await mk({ text: "马上发" });
  const bad = await mk({ text: "也马上发" });
  const cancel = await mk({ sendAt: "2026-09-26T00:00:00Z", text: "取消我" });

  calls = [];
  let r = await P.runScheduledPosts(now + 60_000);
  assert.deepStrictEqual(r, { due: 2, sent: 2, failed: 0 }, "只发到点的");
  assert.ok(calls.every((c) => c.method === "sendMessage" && c.body.chat_id === "-1001"));
  assert.ok(!calls.some((c) => c.body.text === "定时帖"), "未到点不发");

  r = await P.runScheduledPosts(now + 120_000);
  assert.strictEqual(r.due, 0, "不重发");

  await P.cancelPost(cancel.id);
  await assert.rejects(P.cancelPost(asap.id), /还没发出/);

  calls = [];
  failNext = true;
  r = await P.runScheduledPosts(Date.parse("2026-09-27T00:00:00Z"));
  assert.deepStrictEqual(r, { due: 1, sent: 0, failed: 1 }, "到点的只有定时帖；已取消的不发");
  const btn = calls[0].body.reply_markup as { inline_keyboard: { url: string }[][] };
  assert.strictEqual(btn.inline_keyboard[0][0].url, `https://bd2026.cc/r/p/${later.id}`, "https 按钮走站内跳转计点击");
  assert.strictEqual(P.buttonSrc("https://t.me/ctx2026_bot?start=ad_x"), "ad_x");
  assert.strictEqual(P.buttonSrc("https://bd2026.cc/download/chatx?src=ad_y"), "ad_y");
  assert.strictEqual(P.buttonSrc("https://bd2026.cc/download/chatx"), undefined);
  assert.strictEqual((await P.recordPostClick(later.id))?.clicks, 1);
  assert.strictEqual((await P.recordPostClick(later.id))?.clicks, 2, "点击累加");
  assert.strictEqual(await P.recordPostClick(asap.id), null, "无按钮不计");
  assert.strictEqual(P.trackedButtonUrl({ id: 9, button: { text: "t", url: "tg://resolve?domain=x" } }), "tg://resolve?domain=x", "tg:// 原样");
  const all = await P.listPosts();
  const st = (id: number) => all.find((p) => p.id === id)!;
  assert.strictEqual(st(later.id).status, "failed");
  assert.ok(String(st(later.id).error).includes("chat not found"), "记录失败原因");
  assert.strictEqual(st(asap.id).status, "sent");
  assert.strictEqual(st(asap.id).msgId, 555);
  assert.strictEqual(st(bad.id).status, "sent");
  assert.strictEqual(st(cancel.id).status, "canceled");

  await assert.rejects(mk({ photo: "http://x.test/a.jpg" }), /https/);
  await assert.rejects(mk({ photo: "https://x.test/a.jpg", text: "字".repeat(1025) }), /1024/);
  const pic = await mk({ photo: "https://bd2026.cc/og.png", text: "带图帖", buttonText: "下载", buttonUrl: "https://bd2026.cc/download/chatx" });
  calls = [];
  r = await P.runScheduledPosts(now + 60_000);
  assert.deepStrictEqual(r, { due: 1, sent: 1, failed: 0 });
  assert.strictEqual(calls[0].method, "sendPhoto", "有图走 sendPhoto");
  assert.strictEqual(calls[0].body.photo, "https://bd2026.cc/og.png");
  assert.strictEqual(calls[0].body.caption, "带图帖", "内容作图片说明");
  assert.ok(calls[0].body.reply_markup, "带图也带按钮");
  assert.strictEqual((await P.listPosts()).find((p) => p.id === pic.id)?.status, "sent");

  const M = await import("./tg-post-media");
  await assert.rejects(M.saveMedia(Buffer.from("not an image")), /JPG/);
  await assert.rejects(M.saveMedia(Buffer.alloc(0)), /没有/);
  const png = Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), Buffer.alloc(32)]);
  const ref = await M.saveMedia(png);
  assert.ok(M.isMediaRef(ref), "上传返回 media: 引用");
  assert.ok(!M.isMediaRef("media:../../etc/passwd"), "不能路径穿越");
  assert.strictEqual((await M.readMedia(ref))?.type, "image/png");
  const up = await mk({ photo: ref, text: "上传图帖" });
  calls = [];
  r = await P.runScheduledPosts(now + 60_000);
  assert.deepStrictEqual(r, { due: 1, sent: 1, failed: 0 });
  assert.strictEqual(calls[0].method, "sendPhoto", "上传图走 multipart sendPhoto");
  assert.strictEqual(calls[0].body.photo, undefined, "不是按 URL 发");
  assert.strictEqual((await P.listPosts()).find((p) => p.id === up.id)?.status, "sent");
  const lost = await mk({ photo: "media:" + "a".repeat(24) + ".jpg", text: "图丢了" });
  r = await P.runScheduledPosts(now + 60_000);
  assert.strictEqual((await P.listPosts()).find((p) => p.id === lost.id)?.error, "配图文件找不到了");

  await assert.rejects(mk({ repeat: "hourly" }), /每天或每周/);
  const daily = await mk({ text: "每日帖", sendAt: "2026-09-25T11:00:00Z", repeat: "daily" });
  calls = [];
  r = await P.runScheduledPosts(Date.parse("2026-09-27T11:05:00Z"));
  assert.strictEqual(r.sent, 1, "错过的几期只补发一次");
  let next = (await P.listPosts()).filter((p) => p.repeat === "daily" && p.status === "scheduled");
  assert.strictEqual(next.length, 1, "自动排下一期");
  assert.strictEqual(next[0].sendAt, "2026-09-28T11:00:00.000Z", "按原时间点顺延");
  assert.ok(next[0].id > daily.id);
  await P.cancelPost(next[0].id);
  r = await P.runScheduledPosts(Date.parse("2026-09-29T12:00:00Z"));
  next = (await P.listPosts()).filter((p) => p.repeat === "daily" && p.status === "scheduled");
  assert.strictEqual(next.length, 0, "取消后不再排");
  const weekly = await mk({ text: "每周帖", sendAt: "2026-09-25T11:00:00Z", repeat: "weekly" });
  await P.runScheduledPosts(Date.parse("2026-09-25T11:05:00Z"));
  assert.strictEqual((await P.listPosts()).find((p) => p.id > weekly.id && p.repeat === "weekly")?.sendAt, "2026-10-02T11:00:00.000Z");

  // ── 帖子号进 start 载荷：点击 → /start → 下载能串到帖子 ──
  assert.strictEqual(P.postTargetUrl({ id: 12, button: { text: "t", url: "https://t.me/ctx2026_bot?start=ad_x" } }), "https://t.me/ctx2026_bot?start=ad_x__p12", "t.me 深链拼帖子号");
  assert.strictEqual(P.postTargetUrl({ id: 12, button: { text: "t", url: "https://t.me/ctx2026_bot" } }), "https://t.me/ctx2026_bot?start=__p12", "无 start 也带帖子号（src 归 organic）");
  assert.strictEqual(P.postTargetUrl({ id: 12, button: { text: "t", url: "https://t.me/ctx2026_bot?start=ad_x__p3" } }), "https://t.me/ctx2026_bot?start=ad_x__p3", "已带帖子号不重复拼");
  assert.strictEqual(P.postTargetUrl({ id: 12, button: { text: "t", url: "https://t.me/hykj7/5" } }), "https://t.me/hykj7/5", "频道消息链接不动");
  assert.strictEqual(P.postTargetUrl({ id: 12, button: { text: "t", url: "https://bd2026.cc/download/chatx?src=ad_y" } }), "https://bd2026.cc/download/chatx?src=ad_y", "网页链接原样");
  assert.strictEqual(P.postTargetUrl({ id: 12 }), undefined);
  const B = await import("./chatx-bot");
  assert.deepStrictEqual(B.parseStartPayload("/start ad_x__p12"), { src: "ad_x", post: 12 });
  assert.deepStrictEqual(B.parseStartPayload("/start@ctx2026_bot __p12"), { src: "organic", post: 12 });
  assert.deepStrictEqual(B.parseStartPayload("/start ad_x"), { src: "ad_x" });
  assert.deepStrictEqual(B.parseStartPayload("/start"), { src: "organic" });
  assert.strictEqual(B.parseStartSrc("/start ad_x__p12"), "ad_x", "旧口径不带帖子号");

  // ── 重复帖按系列合计：各期共用首期 id ──
  const all2 = await P.listPosts();
  const wk = all2.filter((p) => p.repeat === "weekly");
  assert.strictEqual(wk.length, 2);
  assert.strictEqual(P.seriesOf(wk[0]), weekly.id);
  assert.strictEqual(wk[1].seriesId, weekly.id, "下一期记首期 id");
  assert.strictEqual(P.seriesOf(wk[1]), weekly.id);
  const key = P.seriesKeyFn(all2);
  assert.strictEqual(key(wk[1].id), weekly.id);
  assert.strictEqual(key(999999), 999999, "不认识的原样");
  const sums = P.summarizeSeries(all2);
  const wkS = sums.find((s) => s.seriesId === weekly.id)!;
  assert.deepStrictEqual({ issues: wkS.issues, sent: wkS.sent, failed: wkS.failed, active: wkS.active, repeat: wkS.repeat }, { issues: 2, sent: 1, failed: 0, active: true, repeat: "weekly" });
  const dS = sums.find((s) => s.seriesId === daily.id)!;
  assert.deepStrictEqual({ issues: dS.issues, sent: dS.sent, active: dS.active }, { issues: 2, sent: 1, active: false }, "取消后系列停");
  assert.ok(!sums.some((s) => s.seriesId === asap.id), "非重复帖不进系列表");

  // ── 清理没被引用的上传图 ──
  const orphan = await M.saveMedia(png);
  const fresh = await M.saveMedia(png);
  const orphanName = orphan.slice(6);
  const old = new Date(now - 3 * 86400_000);
  fs.utimesSync(path.join(TMP, "media", orphanName), old, old);
  fs.utimesSync(path.join(TMP, "media", ref.slice(6)), old, old);
  fs.writeFileSync(path.join(TMP, "media", "keep-me.txt"), "x");
  let pr = await P.prunePostMedia(now);
  assert.deepStrictEqual(pr, { scanned: 3, removed: 1, kept: 2 }, "只删过期且无引用的");
  assert.ok(!fs.existsSync(path.join(TMP, "media", orphanName)), "孤儿图删了");
  assert.ok(fs.existsSync(path.join(TMP, "media", ref.slice(6))), "已发帖引用的图保留");
  assert.ok(fs.existsSync(path.join(TMP, "media", fresh.slice(6))), "刚上传的保留");
  assert.ok(fs.existsSync(path.join(TMP, "media", "keep-me.txt")), "非上传命名的文件不碰");
  pr = await P.prunePostMedia(now);
  assert.strictEqual(pr.removed, 0, "幂等");

  console.log("tg-posts ok");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
