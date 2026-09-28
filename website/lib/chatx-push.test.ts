/**
 * ChatX 用户每日早报冒烟：npx tsx lib/chatx-push.test.ts（mock fetch：Telegram / RSS / DeepSeek 都不真打）。
 * 断言：偏好存储原子并发；RSS 解析；DeepSeek 概括只能引用真实条目；产品位 release→tip 轮换只推一次；
 * 受众过滤（/stop、24h 内新 start、当天已推）；小时门 + force；被拉黑自动退订；/news 立发；埋点入日报 pushSent/pushReplied。
 */
import assert from "assert";
import fs from "fs";
import os from "os";
import path from "path";

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "chatx-push-"));
process.env.ANALYTICS_DIR = TMP;
process.env.ANALYTICS_LOG = path.join(TMP, "events.jsonl");
process.env.CHATX_BOT_STARTS_LOG = path.join(TMP, "starts.jsonl");
process.env.CHATX_BOT_PUSH_LOG = path.join(TMP, "push.jsonl");
process.env.CHATX_BOT_PREFS = path.join(TMP, "prefs.json");
process.env.VIDEO_FEED_DB = path.join(TMP, "feed.json");
process.env.CHATX_BOT_TOKEN = "123:CHATX";
process.env.DEEPSEEK_API_KEY = "sk-test";
process.env.TZ_OFFSET = "8";
process.env.CHATX_PUSH_HOUR = "10";
process.env.CHATX_NEWS_FEEDS = "https://feed.example/rss, https://feed.example/atom";

const RSS = `<?xml version="1.0"?><rss><channel>
<item><title><![CDATA[OpenAI 发布 GPT-5 &amp; 新 API]]></title><link>https://n.example/a</link><pubDate>Wed, 23 Sep 2026 01:00:00 GMT</pubDate></item>
<item><title>Old news</title><link>https://n.example/old</link><pubDate>Mon, 01 Sep 2026 01:00:00 GMT</pubDate></item>
<item><title>No link item</title></item>
</channel></rss>`;
const ATOM = `<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Anthropic ships Claude for customer support</title><link href="https://n.example/b"/><updated>2026-09-22T20:00:00Z</updated></entry>
<entry><title>Dup</title><link href="https://n.example/a"/><updated>2026-09-22T21:00:00Z</updated></entry>
</feed>`;

const tg: { method: string; body: Record<string, unknown> }[] = [];
let blockUid: number | null = null;
let dsReply = "1|大模型又升级，客户问题能答得更准\n2|客服场景被大厂验证，AI 回客户是趋势\n9|编造的第九条";
let dsCalls = 0;
globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
  const url = String(input instanceof Request ? input.url : input);
  if (url.startsWith("https://feed.example/rss")) return new Response(RSS, { status: 200 });
  if (url.startsWith("https://feed.example/atom")) return new Response(ATOM, { status: 200 });
  if (url.includes("deepseek")) {
    dsCalls++;
    return new Response(JSON.stringify({ choices: [{ message: { content: dsReply } }] }), { status: 200 });
  }
  if (url.includes("api.telegram.org")) {
    const method = url.split("/").pop()!;
    const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
    tg.push({ method, body });
    if (method === "sendMessage" && blockUid !== null && body.chat_id === blockUid) {
      return new Response(JSON.stringify({ ok: false, error_code: 403, description: "Forbidden: bot was blocked by the user" }), { status: 200 });
    }
    return new Response(JSON.stringify({ ok: true, result: { message_id: 1 } }), { status: 200 });
  }
  throw new Error("unexpected fetch " + url);
}) as typeof fetch;

const sends = () => tg.filter((c) => c.method === "sendMessage");
const at = (day: string, h: number) => Date.parse(`${day}T${String(h).padStart(2, "0")}:15:00+08:00`);

async function main() {
  // ── 偏好存储 ──
  const prefs = await import("./chatx-prefs");
  await Promise.all([prefs.setPrefs(1, { push: false }), prefs.setPrefs(2, { persona: "lover" }), prefs.setPrefs(3, { voice: true })]);
  assert.deepStrictEqual((await prefs.getPrefs(1)).push, false);
  assert.deepStrictEqual((await prefs.getPrefs(2)).persona, "lover");
  assert.strictEqual((await prefs.allPrefs()).size, 3, "并发写不丢");
  await prefs.setPrefs(1, { push: true });
  assert.strictEqual((await prefs.getPrefs(1)).push, true);

  // ── RSS / Atom 解析 ──
  const push = await import("./chatx-push");
  const rss = push.parseFeed(RSS);
  assert.strictEqual(rss.length, 2, "无链接条目丢弃");
  assert.strictEqual(rss[0].title, "OpenAI 发布 GPT-5 & 新 API", "CDATA + 实体还原");
  const atom = push.parseFeed(ATOM);
  assert.strictEqual(atom[0].link, "https://n.example/b", "Atom link href");

  // ── DeepSeek 概括只能引用真实条目 ──
  const items = [
    { title: "A", link: "https://n/a", t: 1 },
    { title: "B", link: "https://n/b", t: 2 },
  ];
  const sum = await push.summarizeNews(items, "zh");
  assert.strictEqual(sum.length, 2, "第 9 条不存在被丢弃");
  assert.strictEqual(sum[0].summary, "大模型又升级，客户问题能答得更准");
  dsReply = "胡说八道没有序号";
  const fb = await push.summarizeNews(items, "zh");
  assert.strictEqual(fb.length, 2);
  assert.strictEqual(fb[0].summary, undefined, "格式不对就只列原标题");
  dsReply = "1|大模型升级\n2|客服场景验证";

  // ── 受众：5 人 start（30 天内），1 人 /stop，1 人刚 start 1 小时，1 人英文 ──
  const D = "2026-09-23";
  const startAt = (uid: number, iso: string, src: string, lang = "zh") => JSON.stringify({ t: iso, uid, src, lang, first: true });
  fs.writeFileSync(
    process.env.CHATX_BOT_STARTS_LOG!,
    [
      startAt(11, "2026-09-20T02:00:00Z", "ad_a"),
      startAt(12, "2026-09-21T02:00:00Z", "ad_a"),
      startAt(13, "2026-09-21T03:00:00Z", "ad_b", "en"),
      startAt(14, "2026-09-22T02:00:00Z", "ad_b"),
      startAt(15, `${D}T01:20:00Z`, "ad_c"), // 1 小时前刚 start，今天不叠
      startAt(16, "2026-07-01T02:00:00Z", "ad_old"), // 超 30 天窗口
    ].join("\n") + "\n"
  );
  await prefs.setPrefs(14, { push: false });
  await prefs.setPrefs(12, { persona: "lover" });

  // 09:15 不到点
  let r = await push.runDailyPush(at(D, 9));
  assert.strictEqual(r.reason, "not_hour");
  assert.strictEqual(sends().length, 0);

  // 10:15 发：11、12、13 收到；14(/stop)、15(刚 start) 跳过；16 不在窗口
  r = await push.runDailyPush(at(D, 10));
  assert.deepStrictEqual({ due: r.due, sent: r.sent, skipped: r.skipped }, { due: 3, sent: 3, skipped: 2 }, JSON.stringify(r));
  const s1 = sends();
  assert.strictEqual(s1.length, 3);
  const zh = s1.find((c) => c.body.chat_id === 11)!;
  const en = s1.find((c) => c.body.chat_id === 13)!;
  const zhText = String(zh.body.text);
  assert.ok(zhText.startsWith("🗞 <b>小界早报 · 09/23</b>"), JSON.stringify(zh.body).slice(0, 600));
  assert.ok(/AI 圈今天/.test(zhText) && /OpenAI 发布 GPT-5 &amp; 新 API/.test(zhText) && /大模型升级/.test(zhText), "含新闻 + 概括：" + zhText);
  assert.ok(!/Old news/.test(zhText), "48h 外的旧闻不出现");
  assert.ok(/ChatX 新版 \d+\.\d+\.\d+/.test(zhText), "首次推最新版本更新日志：" + zhText);
  for (const line of zhText.split("\n")) assert.ok(line.replace(/<[^>]+>/g, "").length <= 100, "单行不超 100 字：" + line);
  assert.ok(/chatx\/releases\?utm_source=telegram&utm_medium=chatx_push/.test(zhText), "更新日志链接带 utm");
  assert.ok(/直接发给我/.test(zhText) && /\/stop/.test(zhText), "邀请回话 + 退订说明");
  assert.strictEqual(zh.body.parse_mode, "HTML");
  const kb = (zh.body.reply_markup as { inline_keyboard: { text: string; url?: string; callback_data?: string }[][] }).inline_keyboard;
  assert.ok(kb[0][1].url!.includes("utm_medium=chatx_push") && kb[0][1].url!.includes("src=ad_a") && kb[0][1].url!.includes("tg=11"), "下载链带 src+uid：" + kb[0][1].url);
  assert.strictEqual(kb[1][0].callback_data, "cx_push_off");
  assert.ok(String(en.body.text).startsWith("🗞 <b>ChatX Daily · 09/23</b>"), "英文用户英文早报");
  assert.ok(/ChatX \d+\.\d+\.\d+ is out/.test(String(en.body.text)), String(en.body.text));
  assert.strictEqual(dsCalls, 2 + 2, "DeepSeek 每语言只概括一次（zh+en），不按人调用");

  // 同一天再跑：不重发
  r = await push.runDailyPush(at(D, 11));
  assert.deepStrictEqual({ due: r.due, sent: r.sent }, { due: 0, sent: 0 });
  assert.strictEqual(sends().length, 3);

  // 埋点 → 日报 pushSent；用户 11 次日回一句 → pushReplied
  // （埋点 t 是真实时间，不是模拟的 D；日报按真实当天看）
  const rep = await import("./chatx-report");
  const realNow = Date.now();
  const realDay = rep.dayKey(new Date(realNow).toISOString());
  fs.appendFileSync(process.env.ANALYTICS_LOG!, JSON.stringify({ t: new Date(realNow + 3600_000).toISOString(), event: "chatx_bot_ai", props: { src: "ad_a", uid: 11, mode: "ai" } }) + "\n");
  const events = await rep.readEvents(process.env.ANALYTICS_LOG!);
  const pushEv = events.filter((e) => e.event === "chatx_bot_push");
  assert.strictEqual(pushEv.length, 3);
  assert.deepStrictEqual(pushEv[0].props?.kind, "release");
  const report = rep.buildDailyReport(events, [realDay], realNow + 2 * 3600_000);
  const total = report.totals.find((t) => t.day === realDay)!;
  assert.strictEqual(total.pushSent, 3);
  assert.strictEqual(total.pushReplied, 1, "推送后 24h 内回话算 replied");

  // 人设只改开场白：恋爱陪聊（12）多一句，小界（11）与从前一样没有
  const byUid = (uid: number) => sends().find((c) => c.body.chat_id === uid)!;
  assert.ok(/想你了 💗/.test(String(byUid(12).body.text)), "恋爱陪聊人设早报带开场白");
  assert.ok(!/想你了/.test(String(byUid(11).body.text)), "默认人设早报不变");
  assert.ok(/AI 圈今天/.test(String(byUid(12).body.text)), "内容不分人设");

  // 次日：版本已推过 → 换成教程小技巧；用户 13 已拉黑 → 自动 push=false
  const D2 = "2026-09-24";
  blockUid = 13;
  r = await push.runDailyPush(at(D2, 10));
  assert.deepStrictEqual({ due: r.due, sent: r.sent }, { due: 4, sent: 3 }, JSON.stringify(r)); // 15 已过 24h 进入受众
  const d2 = sends().slice(3);
  assert.ok(/今日小技巧/.test(String(d2[0].body.text)), "release 只推一次，次日轮到小技巧：" + d2[0].body.text);
  assert.ok(/chatx\/tutorials\?ep=E\d+&utm_source=telegram&utm_medium=chatx_push/.test(String(d2[0].body.text)), "教程深链 ?ep=");
  assert.strictEqual((await prefs.getPrefs(13)).push, false, "被拉黑自动退订");
  blockUid = null;

  // 第三天 13 不再在受众里
  r = await push.runDailyPush(at("2026-09-25", 10));
  assert.strictEqual(r.due, 3, "拉黑用户不再计入");

  // /news：即时重新订阅并发送；/stop：退订
  await push.optOut(11);
  assert.strictEqual((await prefs.getPrefs(11)).push, false);
  const before = sends().length;
  const ok = await push.optInAndSend(11, "ad_a", "zh", at("2026-09-25", 15));
  assert.strictEqual(ok, true);
  assert.strictEqual(sends().length, before + 1);
  assert.strictEqual((await prefs.getPrefs(11)).push, true);
  const last = JSON.parse(fs.readFileSync(process.env.CHATX_BOT_PUSH_LOG!, "utf8").trim().split("\n").pop()!) as { via: string; uid: number };
  assert.deepStrictEqual({ via: last.via, uid: last.uid }, { via: "news", uid: 11 });

  // 强制：force 绕过小时门（当天已推过的人仍不重发）
  r = await push.runDailyPush(at("2026-09-25", 3), true);
  assert.strictEqual(r.reason, undefined);
  assert.strictEqual(r.sent, 0, "force 只是绕过小时门，账本仍生效");

  console.log("chatx-push smoke OK");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
