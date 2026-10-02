/**
 * ChatX 引流 bot 纯冒烟：npx tsx lib/chatx-bot.test.ts（mock fetch，不打 Telegram）。
 * env 必须在动态 import 前设好——lib 在 import 时解析账本路径与 token。
 */
import assert from "assert";
import fs from "fs";
import os from "os";
import path from "path";

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "chatx-bot-"));
process.env.LEADS_DIR = TMP;
process.env.ANALYTICS_DIR = TMP;
process.env.CHATX_BOT_STARTS_LOG = path.join(TMP, "starts.jsonl");
process.env.CHATX_BOT_REMIND_LOG = path.join(TMP, "reminds.jsonl");
process.env.CHATX_BOT_CTX_FILE = path.join(TMP, "ctx.json");
process.env.CHATX_BOT_HERO_CACHE = path.join(TMP, "hero.json");
process.env.CHATX_BOT_DL_LOG = path.join(TMP, "downloads.jsonl");
process.env.ADMIN_CHAT_STORE = path.join(TMP, "admins.json");
process.env.CHATX_BOT_TOKEN = "123:TEST";
// 管理员通知走主 bot（管理员都绑在主 bot 上）
process.env.TELEGRAM_BOT_TOKEN = "999:MAIN";
process.env.TELEGRAM_CHAT_ID = "777";
process.env.CHAT_USAGE = path.join(TMP, "usage.json");
process.env.CHAT_LOG = path.join(TMP, "chats.jsonl");
process.env.DEEPSEEK_BASE_URL = "https://deepseek.test/v1/chat/completions";

type Call = { method: string; body: Record<string, unknown>; bot: "chatx" | "main" };
const calls: Call[] = [];
let photoOk = true;
let llmAnswer: string | null = null;
const llmRequests: Array<Record<string, unknown>> = [];

globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
  const url = String(input);
  if (url.startsWith("https://deepseek.test/")) {
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    llmRequests.push(body);
    if (llmAnswer === null) return new Response("upstream down", { status: 503 });
    return new Response(JSON.stringify({ choices: [{ message: { content: llmAnswer } }] }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }
  const method = url.split("/").pop() ?? "";
  const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {};
  calls.push({ method, body, bot: url.includes("/bot999:MAIN/") ? "main" : "chatx" });
  const ok = method === "sendPhoto" ? photoOk : true;
  // 真 Telegram 的 sendPhoto 会回多尺寸 photo[]；传 file_id 时同样回一份
  const result = ok ? { message_id: calls.length, ...(method === "sendPhoto" ? { photo: [{ file_id: "small" }, { file_id: `fid_${String(body.photo).slice(-8)}` }] } : {}) } : undefined;
  return new Response(JSON.stringify({ ok, result }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}) as typeof fetch;

type Kb = { inline_keyboard: Array<Array<{ text: string; url?: string; callback_data?: string; web_app?: { url: string } }>> };
const flat = (kb: Kb) => kb.inline_keyboard.flat();

async function main() {
  const B = await import("./chatx-bot");
  const from = { id: 4242, username: "tester", first_name: "T" };

  // /start 首条：sendPhoto + 海报 + 键盘
  calls.length = 0;
  await B.handleStart(4242, from, "/start ad_biz_a01", "zh");
  const photo = calls.find((c) => c.method === "sendPhoto");
  assert.ok(photo, "首条应为 sendPhoto");
  assert.match(String(photo!.body.photo), /\/chatx\/bot-hero\?lang=zh&v=[a-z0-9]+$/, "中文默认海报 = 动态路由 + 文案指纹");
  const heroUrlZh = String(photo!.body.photo);
  assert.ok(String(photo!.body.caption).length <= 1024, "caption ≤ 1024");
  const kb = photo!.body.reply_markup as Kb;
  const btns = flat(kb);
  const lastRow = kb.inline_keyboard[kb.inline_keyboard.length - 1];
  assert.ok(lastRow.length === 1 && lastRow[0].url?.includes("/download/chatx") && lastRow[0].url?.includes("src=ad_biz_a01"), "下载深链独占最后一行且带 src");
  assert.ok(lastRow[0].url?.includes("tg=4242"), "下载深链带 Telegram uid，/dl 据此记「谁下载了」");
  assert.ok(!btns.some((b) => b.url?.includes("tg=") && !b.url?.includes("/download/chatx")), "uid 只进下载链，教程/官网链不带");
  assert.ok(btns.some((b) => b.callback_data === "cx_human"), "人工客服是回调（点击能通知管理员），不再是裸外链");
  assert.ok(btns.some((b) => b.url?.includes("/chatx/tutorials") && b.url?.includes("src=ad_biz_a01")), "有教程按钮且带 src");
  assert.ok(btns.some((b) => b.url === "https://t.me/bdccz"), "有官方频道按钮");
  assert.ok(btns.some((b) => /^https:\/\/bd2026\.cc\/\?/.test(b.url ?? "") && b.url?.includes("utm_medium=chatx_bot") && b.url?.includes("utm_campaign=ad_biz_a01")), "有官网按钮且带 utm/来源码");
  assert.ok(!btns.some((b) => b.web_app), "首条键盘不再有小程序按钮（走菜单键）");
  assert.ok(!btns.some((b) => b.url?.includes("/growth")), "不得落到 /growth 泛页");
  assert.match(String(photo!.body.caption), /AI 全自动聊天/, "欢迎语主题 = AI 全自动聊天");
  assert.match(String(photo!.body.caption), /<blockquote>💬 <b>可在这里直接与我聊天<\/b>\n[^<]+<\/blockquote>/, "「可在这里直接与我聊天」用原生 blockquote 重点块 + 加粗");
  assert.ok(!/━/.test(String(photo!.body.caption)), "不再用 ━ 字符线（手机上折行）");
  assert.match(String(photo!.body.caption), /海外：<\/b>Telegram · WhatsApp · LINE · Messenger\n[^\n]*国内：<\/b>微信 · 微信客服 · 抖音 · QQ · 网页客服/, "平台分海外/国内两行；caption 海外行不列 Facebook（Messenger 已代表，防窄屏折行）");
  assert.match(B.featuresText("zh"), /Facebook/, "「能做什么」仍列完整平台（含 Facebook）");
  // 显示宽度（CJK/emoji 算 2、其余算 1）：360px 手机 caption 一行约 44–48 半角宽，平台两行要求不折（≤ 48），其余行最多折一次（≤ 60）
  const width = (s: string) => Array.from(s.replace(/<[^>]+>/g, "")).reduce((n, ch) => n + (/[\u2E80-\u9FFF\uF900-\uFAFF\uFF00-\uFFEF\p{Extended_Pictographic}]/u.test(ch) ? 2 : 1), 0);
  for (const line of String(photo!.body.caption).split("\n")) {
    const limit = /海外：|国内：/.test(line) ? 48 : 60;
    assert.ok(width(line) <= limit, `caption 每行显示宽度 ≤ ${limit} 半角: ${line} (${width(line)})`);
  }
  {
    const emojis = Array.from(String(photo!.body.caption).matchAll(/^(\p{Extended_Pictographic}(?:\uFE0F|\u200D\p{Extended_Pictographic})*)/gmu), (m) => m[1]);
    const kbEmojis = btns.map((b) => b.text.split(" ")[0]);
    const all = [...emojis, ...kbEmojis];
    assert.strictEqual(new Set(all).size, all.length, `caption 行首 + 按钮 emoji 不重复: ${all.join(" ")}`);
  }
  assert.ok(!/API Key|免费下载/.test(String(photo!.body.caption)), "caption 不再有免费/API Key/本机那一行");
  assert.ok(!calls.some((c) => c.method === "sendMessage"), "sendPhoto 成功时不再发文字");
  await new Promise((r) => setTimeout(r, 10));
  const menu = calls.find((c) => c.method === "setChatMenuButton");
  assert.ok(menu && menu.body.chat_id === 4242, "/start 后按会话设菜单键");
  const menuBtn = menu!.body.menu_button as { type: string; web_app: { url: string } };
  assert.strictEqual(menuBtn.type, "web_app");
  assert.ok(menuBtn.web_app.url.includes("/app/chatx") && menuBtn.web_app.url.includes("src=ad_biz_a01"), "菜单键小程序链接带 src");

  // 首触/回访判定 + 账本
  const log = fs.readFileSync(process.env.CHATX_BOT_STARTS_LOG!, "utf-8").trim().split("\n");
  assert.strictEqual(log.length, 1);
  assert.strictEqual((JSON.parse(log[0]) as { src: string }).src, "ad_biz_a01");

  // 第二次同一张图：直接用首次返回的 file_id（不再让 Telegram 回源拉图），且已落盘
  calls.length = 0;
  await B.handleStart(4343, { id: 4343, first_name: "U2" }, "/start ad_biz_a01", "zh");
  const photo2 = calls.filter((c) => c.method === "sendPhoto");
  assert.strictEqual(photo2.length, 1, "命中缓存时只发一次 sendPhoto");
  assert.strictEqual(String(photo2[0].body.photo), `fid_${heroUrlZh.slice(-8)}`, "第二次改用首次返回的最大尺寸 file_id");
  await new Promise((r) => setTimeout(r, 30));
  const heroCache = JSON.parse(fs.readFileSync(process.env.CHATX_BOT_HERO_CACHE!, "utf-8")) as Record<string, string>;
  assert.strictEqual(heroCache[heroUrlZh], `fid_${heroUrlZh.slice(-8)}`, "file_id 落盘，重启后仍可用");

  // sendPhoto 失败 → 回落 sendMessage，键盘保留（英文海报是另一个 URL，无缓存 → 按 URL 发）
  calls.length = 0;
  photoOk = false;
  await B.handleStart(4242, from, "/start", "en");
  const msg = calls.find((c) => c.method === "sendMessage");
  assert.ok(msg, "sendPhoto 失败应回落 sendMessage");
  assert.match(String(calls.find((c) => c.method === "sendPhoto")!.body.photo), /\/chatx\/bot-hero\?lang=en&v=[a-z0-9]+$/, "英文海报带 lang=en");
  assert.ok((msg!.body.reply_markup as Kb).inline_keyboard.length >= 3, "回落消息保留键盘");
  assert.ok(String(msg!.body.text).includes("Welcome back") || String(msg!.body.text).length > 0);
  photoOk = true;

  // /tutorials 命令
  calls.length = 0;
  await B.handleOther(4242, from, "/tutorials", "zh");
  const tut = calls.find((c) => c.method === "sendMessage");
  assert.ok(tut && flat(tut.body.reply_markup as Kb)[0].url?.includes("/chatx/tutorials"), "/tutorials 出教程按钮");

  // 自由文本 → AI 自动回复（与官网同一套 askDeepSeek）：带 ChatX 人设 + 上下文，纯文本发送，键盘带 src
  process.env.DEEPSEEK_API_KEY = "sk-test";
  const aiFrom = { id: 5151, username: "ai_tester", first_name: "A" };
  await B.handleStart(5151, aiFrom, "/start ad_ai_01", "zh");
  llmAnswer = "可以的，ChatX 装好后客户消息 AI 会自动回。";
  calls.length = 0;
  llmRequests.length = 0;
  await B.handleOther(5151, aiFrom, "你们能自动回客户吗", "zh");
  assert.ok(calls.some((c) => c.method === "sendChatAction" && c.body.action === "typing"), "先发 typing");
  const ai = calls.find((c) => c.method === "sendMessage")!;
  assert.strictEqual(ai.body.text, llmAnswer, "回复 = DeepSeek 答案");
  // AI 答案里的裸链接不直发：下载/教程链由键盘承接，其余外链变按钮
  {
    const dl = B.downloadLink("ad_ai_01", "zh");
    const p = B.aiReplyPayload(`可以的。下载安装：${dl}\n案例看 https://example.org/case/1 。`, "ad_ai_01", "zh");
    assert.ok(!/https?:\/\//.test(p.text), `正文不含 URL: ${p.text}`);
    assert.ok(p.text.startsWith("可以的"), "保留回答正文");
    const urls = p.keyboard.flat().map((b) => ("url" in b ? b.url : ""));
    assert.strictEqual(urls.filter((u) => u.includes("/download/chatx")).length, 1, "下载链不重复加按钮（键盘已有）");
    assert.ok(urls.includes("https://example.org/case/1"), "其余外链变成按钮");
    const only = B.aiReplyPayload(dl, "ad_ai_01", "zh");
    assert.ok(only.text.length > 0 && !/https?:/.test(only.text), "纯链接回答也不会发空文本");
  }
  assert.strictEqual(ai.body.parse_mode, undefined, "AI 回复用纯文本，避免 HTML 解析失败");
  const aiBtns = flat(ai.body.reply_markup as Kb);
  assert.ok(aiBtns[aiBtns.length - 1].url?.includes("/download/chatx") && aiBtns[aiBtns.length - 1].url?.includes("src=ad_ai_01"), "AI 回复键盘下载在最后且带原 src");
  assert.ok(!aiBtns.some((b) => b.web_app), "AI 回复键盘也不再有小程序按钮");
  const req1 = llmRequests[0].messages as Array<{ role: string; content: string }>;
  assert.ok(req1[0].role === "system" && /智聊 ChatX/.test(req1[0].content) && /download\/chatx\?[^\s]*src=ad_ai_01/.test(req1[0].content), "system 提示叠了 ChatX 人设 + 带 src 的下载链");
  assert.ok(/你是小界/.test(req1[0].content) && /资料：/.test(req1[0].content), "system 提示保留小界人设 + 官网知识库（与 /api/chat 同源）");
  assert.strictEqual(req1[req1.length - 1].content, "你们能自动回客户吗");
  // 第二轮带上上一轮上下文
  await new Promise((r) => setTimeout(r, 850));
  await B.handleOther(5151, aiFrom, "那价格呢", "zh");
  const req2 = llmRequests[1].messages as Array<{ role: string; content: string }>;
  assert.ok(req2.some((m) => m.role === "assistant" && m.content === llmAnswer), "第二轮带上一轮回答作为上下文");
  // 刷屏限流：紧接着再发不回
  calls.length = 0;
  await B.handleOther(5151, aiFrom, "再问一句", "zh");
  assert.strictEqual(calls.length, 0, "800ms 内连发被限流");
  // 上下文快照落盘（防抖 2s），重启后能续上：模拟新进程 = 重新加载模块
  await new Promise((r) => setTimeout(r, 2300));
  const snap = JSON.parse(fs.readFileSync(process.env.CHATX_BOT_CTX_FILE!, "utf-8")) as Record<string, { turns: Array<{ role: string; content: string }> }>;
  assert.ok(snap["5151"]?.turns.some((m) => m.role === "assistant" && m.content === llmAnswer), "上下文快照已落盘");
  delete require.cache[require.resolve("./chatx-bot")];
  const B2 = require("./chatx-bot") as typeof B;
  assert.notStrictEqual(B2.handleOther, B.handleOther, "确实是全新模块实例（内存上下文为空）");
  await B2.handleOther(5151, aiFrom, "支持外语吗", "zh");
  const req3 = llmRequests[2].messages as Array<{ role: string; content: string }>;
  assert.ok(req3.some((m) => m.role === "assistant" && m.content === llmAnswer), "重新加载后从快照回载上下文");
  // DeepSeek 不可用 → 知识库/兜底，仍有回应
  await new Promise((r) => setTimeout(r, 850));
  llmAnswer = null;
  calls.length = 0;
  await B.handleOther(5151, aiFrom, "hello there", "en");
  const fb = calls.find((c) => c.method === "sendMessage")!;
  assert.ok(fb && String(fb.body.text).length > 0, "AI 挂了也有回应");
  assert.match(String(fb.body.text), /AI is busy/, "不可用时是 ChatX 专属「系统忙」提示");
  assert.ok(!/Mini App|小程序|didn't quite/i.test(String(fb.body.text)), "不再走官网通用兜底文案");
  assert.ok(flat(fb.body.reply_markup as Kb).some((b) => b.url?.includes("/download/chatx")), "忙提示仍带下载按钮");
  // 非文字消息不静默
  await new Promise((r) => setTimeout(r, 850));
  calls.length = 0;
  await B.handleNonText(5151, aiFrom, "voice", "zh");
  const nt = calls.find((c) => c.method === "sendMessage")!;
  assert.match(String(nt.body.text), /语音/, "语音消息有回应且点明类型");
  assert.ok(flat(nt.body.reply_markup as Kb).some((b) => b.url?.includes("src=ad_ai_01")), "非文字回应键盘仍带 src");
  // 名片 → 留资（带 src 归因）
  calls.length = 0;
  await B.handleContact(5151, aiFrom, { phone_number: "+8613800000000", first_name: "A" }, "zh");
  assert.match(String(calls.find((c) => c.method === "sendMessage" && c.bot === "chatx")!.body.text), /收到/);
  assert.ok(calls.some((c) => c.method === "sendMessage" && c.bot === "main" && String(c.body.chat_id) === "777"), "名片留资通知管理员（主 bot）");
  const leads = fs.readFileSync(path.join(TMP, "leads.jsonl"), "utf-8").trim().split("\n").map((l) => JSON.parse(l) as { contact: string; source: string; utm?: string });
  const lead = leads.find((l) => l.contact.includes("+8613800000000"));
  assert.ok(lead && lead.source === "chatx_bot" && lead.utm === "telegram/chatx_bot/ad_ai_01", "名片落线索库且 utm 带 src");
  // 未知命令不走 AI，回菜单
  await new Promise((r) => setTimeout(r, 850));
  llmRequests.length = 0;
  calls.length = 0;
  await B.handleOther(5151, aiFrom, "/whatever", "zh");
  assert.strictEqual(llmRequests.length, 0, "未知命令不调 AI");
  assert.ok(calls.some((c) => c.method === "sendMessage"), "未知命令回菜单");
  delete process.env.DEEPSEEK_API_KEY;

  // 人工客服回调：用户拿到直达链 + 一键留资按钮；管理员经主 bot 收到通知（带身份 + 来源）
  calls.length = 0;
  await B.handleCallback(5151, "cq1", "cx_human", aiFrom, "zh");
  assert.ok(calls.some((c) => c.method === "answerCallbackQuery"), "回调先 answer");
  const hm = calls.find((c) => c.method === "sendMessage" && c.bot === "chatx" && c.body.chat_id === 5151)!;
  assert.match(String(hm.body.text), /人工/);
  const hmBtns = flat(hm.body.reply_markup as Kb);
  assert.ok(hmBtns.some((b) => b.url === "https://t.me/WJKJ2026"), "有人工直达链");
  assert.ok(hmBtns.some((b) => b.callback_data === "cx_lead"), "有「让客服联系我」一键留资");
  const adminMsg = calls.find((c) => c.method === "sendMessage" && c.bot === "main")!;
  assert.ok(adminMsg && String(adminMsg.body.chat_id) === "777", "管理员通知经主 bot token 发到管理员 chat");
  assert.match(String(adminMsg.body.text), /ChatX bot/);
  assert.match(String(adminMsg.body.text), /@ai_tester/, "通知带用户身份");
  assert.match(String(adminMsg.body.text), /ad_ai_01/, "通知带来源码");
  // 一键留资：落线索库（src / uid）+ 首次通知管理员 + 用户确认（换一个没留过名片的用户，5151 已按 uid 去重）
  const leadFrom = { id: 6161, username: "quick_tester", first_name: "Q" };
  await B.handleStart(6161, leadFrom, "/start ad_lead_01", "zh");
  calls.length = 0;
  await B.handleCallback(6161, "cq2", "cx_lead", leadFrom, "zh");
  const lq = calls.find((c) => c.method === "sendMessage" && c.bot === "chatx" && c.body.chat_id === 6161)!;
  assert.match(String(lq.body.text), /已记下/);
  const leads2 = fs.readFileSync(path.join(TMP, "leads.jsonl"), "utf-8").trim().split("\n").map((l) => JSON.parse(l) as { contact: string; source: string; utm?: string; tg_user_id?: string; interest: string });
  const quick = leads2.find((l) => l.contact === "@quick_tester" && /让客服联系我/.test(l.interest));
  assert.ok(quick && quick.source === "chatx_bot" && quick.utm === "telegram/chatx_bot/ad_lead_01" && quick.tg_user_id === "6161", "一键留资落库带 src + uid");
  const leadNotify = calls.find((c) => c.method === "sendMessage" && c.bot === "main")!;
  assert.ok(leadNotify && /@quick_tester/.test(String(leadNotify.body.text)), "新线索通知管理员");
  // 再点一次：不重复通知（upsert 非新线索），用户仍有确认
  calls.length = 0;
  await B.handleCallback(6161, "cq3", "cx_lead", leadFrom, "zh");
  assert.ok(calls.some((c) => c.method === "sendMessage" && c.bot === "chatx"), "重复点击仍回确认");
  assert.strictEqual(calls.filter((c) => c.method === "sendMessage" && c.bot === "main").length, 0, "重复点击不再打扰管理员");
  // /support 命令等价于人工客服回调
  calls.length = 0;
  await B.handleOther(5151, aiFrom, "/support", "en");
  assert.ok(flat(calls.find((c) => c.method === "sendMessage" && c.bot === "chatx")!.body.reply_markup as Kb).some((b) => b.callback_data === "cx_lead"));
  // uid 只在 from.id 合法时进链
  assert.ok(!B.downloadLink("ad_x", "zh", "chatx_bot", 0).includes("tg="), "uid=0 不进链");
  assert.ok(!B.downloadLink("ad_x", "zh", "chatx_bot", 1.5).includes("tg="), "非整数不进链");
  assert.ok(B.downloadLink("ad_x", "zh", "chatx_bot", 5151).includes("tg=5151"));
  assert.ok(!B.downloadLink("ad_x", "zh").includes("tg="), "不传 uid 与从前一致");

  // 非法 src 归一
  assert.strictEqual(B.parseStartSrc("/start <script>"), "organic");
  assert.strictEqual(B.parseStartSrc("/start@ctx2026_bot ad_x-1"), "ad_x-1");

  // 24h 追发：只发给「单次 /start 且 24h–72h 前」的用户，且一生一次
  const H = 3600_000;
  const now = Date.now();
  const at = (h: number) => new Date(now - h * H).toISOString();
  fs.writeFileSync(
    process.env.CHATX_BOT_STARTS_LOG!,
    [
      { t: at(30), uid: 1001, src: "ad_a", lang: "zh" }, // 应发
      { t: at(26), uid: 1002, src: "ad_b", lang: "en" }, // 应发（英文）
      { t: at(5), uid: 1003, src: "ad_c", lang: "zh" }, // 未满 24h
      { t: at(100), uid: 1004, src: "ad_d", lang: "zh" }, // 超 72h 不补发
      { t: at(40), uid: 1005, src: "ad_e", lang: "zh" }, // 下面又 start 了一次 → 已主动回来，不发
      { t: at(2), uid: 1005, src: "ad_e", lang: "zh" },
      { t: at(28), uid: 1006, src: "ad_f", lang: "zh" }, // 已请求过安装包（下面的下载回执）→ 跳过
    ]
      .map((r) => JSON.stringify(r))
      .join("\n") + "\n"
  );
  const DL = await import("./chatx-dl-ledger");
  await DL.recordBotDownload({ uid: 1006, src: "ad_f", file: "ChatX-Setup.exe" });
  assert.ok(DL.isValidTgUid("1006") && !DL.isValidTgUid("1e3") && !DL.isValidTgUid("") && !DL.isValidTgUid(null), "uid 只认纯数字");
  assert.ok((await DL.downloadedUids()).has(1006));
  calls.length = 0;
  const r1 = await B.runStartReminders(now);
  assert.deepStrictEqual(r1, { due: 2, sent: 2, skipped: 1 });
  assert.ok(!calls.some((c) => c.method === "sendMessage" && c.body.chat_id === 1006), "已下载的用户不再提醒");
  const sentTo = calls.filter((c) => c.method === "sendMessage").map((c) => c.body.chat_id);
  assert.deepStrictEqual(sentTo.sort(), [1001, 1002]);
  const rm = calls.find((c) => c.method === "sendMessage" && c.body.chat_id === 1001)!;
  const rmBtn = flat(rm.body.reply_markup as Kb)[0];
  assert.ok(rmBtn.url?.includes("src=ad_a") && rmBtn.url?.includes("utm_medium=chatx_bot_remind") && rmBtn.url?.includes("tg=1001"), "提醒下载链带 src + remind medium + uid");
  const rmEn = calls.find((c) => c.method === "sendMessage" && c.body.chat_id === 1002)!;
  assert.match(String(rmEn.body.text), /installed/);
  // 再跑一次：已发的不重发
  calls.length = 0;
  assert.deepStrictEqual(await B.runStartReminders(now), { due: 0, sent: 0, skipped: 1 });
  assert.strictEqual(calls.filter((c) => c.method === "sendMessage").length, 0);
  // 1003 满 24h 后轮到
  assert.deepStrictEqual(await B.runStartReminders(now + 20 * H), { due: 1, sent: 1, skipped: 1 });

  // recentStarts：每人取最近一次、窗口过滤、src 归一
  const rs = await B.recentStarts(now - 72 * H);
  assert.ok(!rs.has(1004), "72h 外不在窗口");
  assert.strictEqual(rs.get(1005)?.lastT, Date.parse(at(2)), "同一人取最近一次 start");
  assert.strictEqual(rs.get(1001)?.src, "ad_a");

  console.log("chatx-bot smoke OK");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
