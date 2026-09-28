/**
 * ChatX 管理员日报冒烟：npx tsx lib/chatx-daily-digest.test.ts（mock fetch，不打 Telegram）。
 * 断言：非设定小时不发；到点发一次且内容含昨天的数字；同一天第二次不重发；force 绕过小时门。
 */
import assert from "assert";
import fs from "fs";
import os from "os";
import path from "path";

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "chatx-digest-"));
process.env.ANALYTICS_DIR = TMP;
process.env.ANALYTICS_LOG = path.join(TMP, "events.jsonl");
process.env.CHATX_DIGEST_LOG = path.join(TMP, "digest.jsonl");
process.env.ADMIN_CHAT_STORE = path.join(TMP, "admins.json");
process.env.TELEGRAM_BOT_TOKEN = "999:MAIN";
process.env.TELEGRAM_CHAT_ID = "777";
process.env.TZ_OFFSET = "8";
process.env.CHATX_DIGEST_HOUR = "9";
process.env.CHATX_TICKETS_FILE = path.join(TMP, "tickets.json");
process.env.TG_HUB_FILE = path.join(TMP, "tg_hub.json");
process.env.TG_POSTS_FILE = path.join(TMP, "tg_posts.json");

const sent: { chat: unknown; text: string }[] = [];
globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
  const url = String(input instanceof Request ? input.url : input);
  if (url.includes("api.telegram.org") && url.endsWith("/sendMessage")) {
    const body = JSON.parse(String(init?.body ?? "{}")) as { chat_id: unknown; text: string };
    sent.push({ chat: body.chat_id, text: body.text });
    return new Response(JSON.stringify({ ok: true, result: { message_id: 1 } }), { status: 200 });
  }
  return new Response(JSON.stringify({ ok: true }), { status: 200 });
}) as typeof fetch;

async function main() {
  const { runDailyDigest, runWeeklyDigest } = await import("./chatx-daily-digest");

  // 昨天（本地 +8）：ad_x 2 人 start、1 人落地、1 人请求包
  const yday = "2026-09-22";
  const ev = (tLocal: string, event: string, props: Record<string, unknown>, extra: Record<string, unknown> = {}) =>
    JSON.stringify({ t: new Date(`${yday}T${tLocal}+08:00`).toISOString(), event, props, ...extra });
  fs.writeFileSync(
    process.env.ANALYTICS_LOG!,
    [
      ev("10:00:00", "chatx_bot_start", { src: "ad_x", uid: 1, first: true }),
      ev("10:05:00", "chatx_bot_start", { src: "ad_x", uid: 2, first: true }),
      ev("10:06:00", "chatx_landing_view", { src: "ad_x", tg: 1 }, { ip: "9.9.9.9" }),
      ev("10:07:00", "download_redirect", { src: "ad_x", uid: 1, installer: true }, { ip: "9.9.9.9" }),
      ev("10:08:00", "chatx_bot_ai", { src: "ad_x", uid: 2, mode: "ai" }),
      ev("11:00:00", "chatx_bot_bug", { step: "note", src: "ad_x", uid: 1, known: "smartscreen" }),
      ev("11:01:00", "chatx_bot_bug", { step: "solved", src: "ad_x", uid: 1, known: "smartscreen" }),
      ev("11:02:00", "chatx_bot_bug", { step: "fp", src: "ad_x", uid: 2, known: "network" }),
      ev("11:03:00", "chatx_bot_bug", { step: "escalate", src: "ad_x", uid: 2, known: "network" }),
      ev("12:00:00", "tg_post_click", { post: 7, chat: "-1001", src: "ad_x" }),
      ev("12:01:00", "tg_post_click", { post: 7, chat: "-1001", src: "ad_x" }),
      ev("12:02:00", "tg_post_click", { post: 8, chat: "-1001" }),
    ].join("\n") + "\n"
  );

  // 昨天 2 张工单：一张 5 分钟回复，一张 40 分钟回复（超 15 分钟 SLA）
  const tk = (id: number, created: string, replied: string) => ({ id, botId: "chatx", uid: id, chatId: id, name: "u", src: "ad_x", lang: "zh", kind: "human", status: "resolved", supportBotId: "chatx", supportChatId: "-1", msgIds: [], createdAt: new Date(`${yday}T${created}+08:00`).toISOString(), updatedAt: "", firstReplyAt: new Date(`${yday}T${replied}+08:00`).toISOString(), rating: "y" });
  fs.writeFileSync(process.env.CHATX_TICKETS_FILE!, JSON.stringify({ seq: 2, tickets: [tk(1, "12:00:00", "12:05:00"), tk(2, "13:00:00", "13:40:00")] }));

  const at = (hLocal: number) => Date.parse(`2026-09-23T${String(hLocal).padStart(2, "0")}:10:00+08:00`);

  // 08:10 不到点
  let r = await runDailyDigest(at(8));
  assert.deepStrictEqual(r, { day: yday, sent: false, reason: "not_hour" });
  assert.strictEqual(sent.length, 0);

  // 09:10 发一次
  r = await runDailyDigest(at(9));
  assert.strictEqual(r.sent, true, "到点发送");
  assert.strictEqual(sent.length, 1);
  assert.strictEqual(String(sent[0].chat), "777", "发到管理员 chat");
  const txt = sent[0].text;
  assert.ok(txt.includes(`ChatX 广告日报 · ${yday}`), "标题带昨天日期");
  assert.ok(txt.includes("进 bot <b>2</b> 人"), "进人 2");
  assert.ok(txt.includes("到落地 <b>1</b> 人"), "落地 1");
  assert.ok(txt.includes("请求安装包 <b>1</b> 人"), "下载人 1");
  assert.ok(txt.includes("1 人聊了 1 句"), "对话 1/1");
  assert.ok(txt.includes("<code>ad_x</code>  2 → 1 → <b>1</b>"), "按来源行");
  assert.ok(txt.includes("💡 已知问题：给出 2 次 · 解决 1 · 仍转人工 1"), "已知问题命中");
  assert.ok(txt.includes("📣 帖子（点击→进 bot→下载人）：3→0→0（#7 2→0→0 · #8 1→0→0）"), "帖子点击");
  assert.ok(txt.includes("smartscreen 1→✅1/👤0") && txt.includes("network 1→✅0/👤1"), "按条目");
  assert.ok(txt.includes("🎫 客服：新工单 2 · 已回复 2") && txt.includes("超 15 分钟 1（50%）"), "客服 SLA 行");

  // 同一小时 cron 每 10 分钟再打：不重发
  r = await runDailyDigest(at(9) + 10 * 60_000);
  assert.deepStrictEqual(r, { day: yday, sent: false, reason: "already_sent" });
  assert.strictEqual(sent.length, 1, "幂等");

  // force：绕过小时门，立即再发一份（手动 ?digest=1）
  r = await runDailyDigest(at(15), true);
  assert.strictEqual(r.sent, true);
  assert.strictEqual(sent.length, 2);

  // 次日 09 点：发新的一天（前一天空数据也要发「没有数据」）
  r = await runDailyDigest(Date.parse("2026-09-24T09:05:00+08:00"));
  assert.strictEqual(r.day, "2026-09-23");
  assert.strictEqual(r.sent, true);
  assert.ok(sent[2].text.includes("没有任何"), "空日报明示无数据");

  // ── 周报：2026-09-28 周一 09 点发上周（09-21 ~ 09-27）；前一周 ad_x 有 1 人 ──
  fs.appendFileSync(
    process.env.ANALYTICS_LOG!,
    [
      JSON.stringify({ t: new Date("2026-09-16T10:00:00+08:00").toISOString(), event: "chatx_bot_start", props: { src: "ad_x", uid: 5, first: true } }),
      JSON.stringify({ t: new Date("2026-09-23T10:00:00+08:00").toISOString(), event: "chatx_bot_start", props: { src: "ad_x", uid: 1, first: false, post: 7 } }),
      JSON.stringify({ t: new Date("2026-09-23T10:01:00+08:00").toISOString(), event: "chatx_bot_voice_in", props: { src: "ad_x", uid: 1, ok: true, sec: 3 } }),
      JSON.stringify({ t: new Date("2026-09-23T10:02:00+08:00").toISOString(), event: "chatx_bot_ai", props: { src: "ad_x", uid: 1, mode: "ai", persona: "lover" } }),
    ].join("\n") + "\n"
  );
  const mon = (h: number) => Date.parse(`2026-09-28T${String(h).padStart(2, "0")}:10:00+08:00`);
  const n0 = sent.length;
  let w = await runWeeklyDigest(Date.parse("2026-09-27T09:10:00+08:00"));
  assert.deepStrictEqual(w, { week: "2026-09-20", sent: false, reason: "not_time" }, "周日不发");
  w = await runWeeklyDigest(mon(8));
  assert.strictEqual(w.reason, "not_time", "周一非设定小时不发");
  w = await runWeeklyDigest(mon(9));
  assert.deepStrictEqual(w, { week: "2026-09-21", sent: true });
  const wt = sent[sent.length - 1].text;
  assert.strictEqual(sent.length, n0 + 1);
  assert.ok(wt.includes("ChatX 周报 · 09-21 ~ 09-27"), wt);
  assert.ok(wt.includes("进 bot <b>2</b> 人（新 2）· 较上周 +1"), "周内 u1 两天都来只算 1 人；上周 1 人");
  assert.ok(wt.includes("请求安装包 <b>1</b> 人（较上周 +1）"), wt);
  assert.ok(wt.includes("📈 每日进人→下载：09-21 0→0 · 09-22 2→1 · 09-23 1→0"), wt);
  assert.ok(wt.includes("🎙 语音：收到 1 条"), wt);
  assert.ok(/🎭 人设：.*恋爱陪聊 1 句/.test(wt), wt);
  assert.ok(wt.includes("💡 已知问题：给出 2 次 · 解决 1 · 仍转人工 1"), wt);
  assert.ok(wt.includes("📣 帖子/系列（点击→进 bot→下载人）：3→1→1"), wt);
  assert.ok(wt.includes("🎫 客服：新工单 2"), "周客服行");
  assert.ok(wt.includes("<code>ad_x</code>  2 → 1 → <b>1</b>  +1"), wt);
  w = await runWeeklyDigest(mon(9) + 10 * 60_000);
  assert.deepStrictEqual(w, { week: "2026-09-21", sent: false, reason: "already_sent" }, "同周不重发");
  w = await runWeeklyDigest(mon(15), true);
  assert.strictEqual(w.sent, true, "force 立即发");
  r = await runDailyDigest(mon(9) + 20 * 60_000);
  assert.deepStrictEqual(r, { day: "2026-09-27", sent: true }, "周报账本不挡同小时的日报");

  console.log("chatx-daily-digest smoke OK");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
