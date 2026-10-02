/**
 * ChatX 客服 / 报障 / 群管理冒烟：npx tsx lib/chatx-support.test.ts（mock fetch，不打 Telegram）。
 */
import assert from "assert";
import fs from "fs";
import os from "os";
import path from "path";

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "chatx-support-"));
process.env.LEADS_DIR = TMP;
process.env.ANALYTICS_DIR = TMP;
process.env.CHATX_BOT_STARTS_LOG = path.join(TMP, "starts.jsonl");
process.env.CHATX_BOT_REMIND_LOG = path.join(TMP, "reminds.jsonl");
process.env.CHATX_BOT_CTX_FILE = path.join(TMP, "ctx.json");
process.env.CHATX_BOT_HERO_CACHE = path.join(TMP, "hero.json");
process.env.CHATX_BOT_DL_LOG = path.join(TMP, "downloads.jsonl");
process.env.ADMIN_CHAT_STORE = path.join(TMP, "admins.json");
process.env.TG_HUB_FILE = path.join(TMP, "tg_hub.json");
process.env.CHATX_TICKETS_FILE = path.join(TMP, "tickets.json");
process.env.CHATX_KNOWN_ISSUES_FILE = path.join(TMP, "known_issues.json");
process.env.CLIENT_LOG_PATH = path.join(TMP, "client-logs.jsonl");
process.env.CHATX_BUG_FOLLOWUP_FILE = path.join(TMP, "followups.json");
process.env.CHATX_BOT_TOKEN = "123:TEST";
process.env.NEXT_PUBLIC_CHATX_BOT_HANDLE = "ctx2026_bot";
process.env.TELEGRAM_BOT_TOKEN = "999:MAIN";
process.env.TELEGRAM_CHAT_ID = "777";
process.env.CHAT_USAGE = path.join(TMP, "usage.json");
process.env.CHAT_LOG = path.join(TMP, "chats.jsonl");
process.env.DEEPSEEK_API_KEY = "k";
process.env.DEEPSEEK_BASE_URL = "https://deepseek.test/v1/chat/completions";

const SUPPORT = -100500;
const COMMUNITY = -100600;
const COMMUNITY2 = -100700;
const USER = 4242;

fs.writeFileSync(
  process.env.TG_HUB_FILE,
  JSON.stringify({
    bots: [],
    chats: [
      { chatId: String(SUPPORT), botId: "chatx", title: "客服群", type: "supergroup", isForum: true, role: "support", enabled: true, botStatus: "administrator", features: {}, discoveredAt: "", updatedAt: "" },
      { chatId: String(COMMUNITY2), botId: "chatx", title: "主动答疑群", type: "supergroup", role: "community", enabled: true, botStatus: "administrator", features: {}, discoveredAt: "", updatedAt: "" },
      { chatId: String(COMMUNITY), botId: "chatx", title: "交流群", type: "supergroup", role: "community", enabled: true, botStatus: "administrator", features: {}, discoveredAt: "", updatedAt: "" },
      { chatId: String(COMMUNITY2), botId: "chatx", title: "主动答疑群", type: "supergroup", role: "community", enabled: true, botStatus: "administrator", features: {}, discoveredAt: "", updatedAt: "" },
    ],
    invites: [],
    support: { hours: "00:00-00:00", tzOffset: 8 },
  })
);

type Call = { method: string; body: Record<string, unknown> };
let calls: Call[] = [];
let llmCalls = 0;
let llmReply = "点官网下载就行。";

globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
  const url = String(input);
  if (url.startsWith("https://deepseek.test/")) {
    llmCalls += 1;
    return new Response(JSON.stringify({ choices: [{ message: { content: llmReply } }] }), { status: 200, headers: { "Content-Type": "application/json" } });
  }
  const method = url.split("/").pop() ?? "";
  const body = init?.body && typeof init.body === "string" ? (JSON.parse(init.body) as Record<string, unknown>) : {};
  calls.push({ method, body });
  let result: unknown = { message_id: 1000 + calls.length };
  if (method === "createForumTopic") result = { message_thread_id: 77 };
  if (method === "getChatMember") result = { status: "member" };
  return new Response(JSON.stringify({ ok: true, result }), { status: 200, headers: { "Content-Type": "application/json" } });
}) as typeof fetch;

const from = { id: USER, first_name: "Ann", username: "ann", language_code: "zh" };
const sent = (chatId: number) => calls.filter((c) => c.method === "sendMessage" && c.body.chat_id === chatId);
const lastText = (chatId: number) => String(sent(chatId).at(-1)?.body.text ?? "");
const kb = (c: Call | undefined) => ((c?.body.reply_markup as { inline_keyboard?: { callback_data?: string; url?: string }[][] })?.inline_keyboard ?? []).flat();

async function main() {
  const W = await import("./chatx-webhook");
  const S = await import("./chatx-support");
  const H = await import("./tg-hub-store");
  const T = await import("./chatx-tickets");
  const { normalizeFingerprint } = await import("./ai-gateway");
  let uid = 1;
  const upd = (u: Record<string, unknown>) => W.handleChatxUpdate({ update_id: uid++, ...u } as never);
  const pm = (text: string) => upd({ message: { message_id: uid, chat: { id: USER, type: "private" }, from, text } });
  const cb = (data: string, chatId = USER, type = "private", who = from) => upd({ callback_query: { id: `cq${uid}`, data, from: who, message: { message_id: 5, chat: { id: chatId, type } } } });

  // 识别
  const fp = normalizeFingerprint("ABCD-1234-EF56-7890");
  assert.strictEqual(S.extractFingerprint("我的机器码 abcd-1234-ef56-7890 谢谢"), fp);
  assert.strictEqual(S.extractFingerprint("没有机器码"), null);
  assert.strictEqual(H.inSupportHours({ hours: "09:00-22:00", tzOffset: 8 }, Date.parse("2026-09-24T02:00:00Z")), true);
  assert.strictEqual(H.inSupportHours({ hours: "09:00-22:00", tzOffset: 8 }, Date.parse("2026-09-24T15:00:00Z")), false);
  assert.strictEqual(H.inSupportHours({ hours: "20:00-02:00", tzOffset: 0 }, Date.parse("2026-09-24T01:00:00Z")), true);

  // 多客服群分流：语言 > 同 bot > 未结工单少
  {
    const base = { type: "supergroup", role: "support" as const, enabled: true, botStatus: "administrator", features: H.DEFAULT_FEATURES, discoveredAt: "", updatedAt: "", title: "" };
    const hub = { bots: [], invites: [], support: H.DEFAULT_SUPPORT, chats: [
      { ...base, chatId: "-1", botId: "chatx" },
      { ...base, chatId: "-2", botId: "chatx", langs: ["en"] },
      { ...base, chatId: "-3", botId: "bX" },
      { ...base, chatId: "-4", botId: "chatx", enabled: false, langs: ["zh"] },
    ] };
    assert.strictEqual(H.pickSupportChat(hub, "chatx", "en")?.chatId, "-2", "英文用户 → 英文客服群");
    assert.strictEqual(H.pickSupportChat(hub, "chatx", "zh")?.chatId, "-1", "中文用户 → 不限语言、同 bot；停用的不选");
    assert.strictEqual(H.pickSupportChat(hub, "bX", "zh")?.chatId, "-3", "同 bot 优先");
    assert.strictEqual(H.pickSupportChat(hub, "chatx", "zh", { "chatx:-1": 5, "bX:-3": 0 })?.chatId, "-1", "同 bot 优先于负载");
    const hub2 = { ...hub, chats: [{ ...base, chatId: "-5", botId: "chatx" }, { ...base, chatId: "-6", botId: "chatx" }] };
    assert.strictEqual(H.pickSupportChat(hub2, "chatx", "zh", { "chatx:-5": 3, "chatx:-6": 1 })?.chatId, "-6", "同级选未结工单少的");
  }

  fs.writeFileSync(process.env.CLIENT_LOG_PATH!, JSON.stringify({ t: new Date().toISOString(), fp, ver: "1.0.50", logger: "app", level: "ERROR", msg: "ConnectionError: timed out", n: 3 }) + "\n");

  // /bug → 选类型
  await pm("/bug");
  assert.ok(kb(sent(USER).at(-1)).some((b) => b.callback_data === "cxb:k:crash"), "/bug 给出问题类型按钮");
  await cb("cxb:k:crash");
  assert.match(lastText(USER), /机器码/);
  assert.match(lastText(USER), /不要发到群里/);

  // 机器码 → 设备摘要 + 过期版本 + 已知问题
  await pm(`机器码 ${fp}`);
  const summary = lastText(USER);
  assert.match(summary, /1\.0\.50/);
  assert.match(summary, /不是最新/);
  assert.match(summary, /网络/, "错误摘要命中已知问题（网络超时）");
  assert.ok(kb(sent(USER).at(-1)).some((b) => b.callback_data === "cxb:h"));
  assert.strictEqual(llmCalls, 0, "机器码不走 AI");

  // 已知问题追问：30 分钟内不追问；之后追问一次；只追问一次
  calls = [];
  assert.strictEqual((await S.runKnownFollowups(Date.now() + 10 * 60000)).asked, 0, "未满 30 分钟不追问");
  assert.strictEqual((await S.runKnownFollowups(Date.now() + 31 * 60000)).asked, 1, "没点按钮 → 追问");
  assert.match(lastText(USER), /解决了吗/);
  assert.ok(kb(sent(USER).at(-1)).some((b) => b.callback_data === "cxb:ok"), "追问带「解决了 / 转人工」");
  assert.strictEqual((await S.runKnownFollowups(Date.now() + 40 * 60000)).asked, 0, "只追问一次");

  // 转人工 → 话题 + 工单卡
  calls = [];
  await cb("cxb:h");
  assert.ok(calls.some((c) => c.method === "createForumTopic" && c.body.chat_id === String(SUPPORT)), "在客服群开话题");
  const card = calls.find((c) => c.method === "sendMessage" && c.body.chat_id === String(SUPPORT));
  assert.ok(card, "工单卡发到客服群");
  assert.strictEqual(card!.body.message_thread_id, 77);
  assert.match(String(card!.body.text), new RegExp(fp));
  assert.ok(kb(card).some((b) => b.callback_data === "tk:c:1"));
  assert.match(lastText(USER), /工单 #1/);

  // 客服还没接：用户消息转过去，AI 照常回答
  calls = [];
  await pm("还是闪退");
  assert.ok(calls.some((c) => c.method === "copyMessage" && c.body.chat_id === String(SUPPORT) && c.body.message_thread_id === 77), "用户消息转进话题");

  // 客服在话题里回复 → 转给用户，工单变处理中
  calls = [];
  await upd({ message: { message_id: 900, message_thread_id: 77, chat: { id: SUPPORT, type: "supergroup", is_forum: true }, from: { id: 1, first_name: "Kefu" }, text: "请重装试试" } });
  const toUser = sent(USER).at(-1);
  assert.ok(toUser && String(toUser.body.text).includes("客服：请重装试试"), "客服回复转给用户");
  assert.strictEqual((await T.activeTicketFor("chatx", USER))?.status, "claimed");

  // 客服已接手：用户消息只转发，不再调 AI
  calls = [];
  const before = llmCalls;
  await pm("好的我试试");
  assert.ok(calls.some((c) => c.method === "copyMessage"));
  assert.strictEqual(llmCalls, before, "客服接手后 AI 不插话");
  assert.strictEqual(sent(USER).length, 0);

  // 非客服群里的人点结案按钮无效；客服群里点有效
  await cb("tk:r:1", USER, "private");
  assert.strictEqual((await T.activeTicketFor("chatx", USER))?.status, "claimed", "用户不能代替客服结案");
  calls = [];
  await cb("tk:r:1", SUPPORT, "supergroup", { id: 1, first_name: "Kefu", username: "kefu", language_code: "zh" });
  assert.ok(calls.some((c) => c.method === "closeForumTopic"));
  assert.match(lastText(USER), /解决了吗/);
  assert.strictEqual(await T.activeTicketFor("chatx", USER), undefined);
  await cb("tk:n:1");
  assert.strictEqual((await T.activeTicketFor("chatx", USER))?.status, "open", "用户反馈没解决 → 重开");
  assert.ok(calls.some((c) => c.method === "reopenForumTopic"));

  // 群：机器码自动删并引导私聊
  calls = [];
  const gFrom = { id: 555, first_name: "Bob", language_code: "zh" };
  await upd({ message: { message_id: 31, chat: { id: COMMUNITY, type: "supergroup" }, from: gFrom, text: `帮我看看 ${fp}` } });
  assert.ok(calls.some((c) => c.method === "deleteMessage" && c.body.message_id === 31), "群里的机器码被删");
  assert.ok(kb(sent(COMMUNITY).at(-1)).some((b) => b.url?.includes("start=bug")));

  // 群：不 @ 不说话；@ 了才答
  calls = [];
  await upd({ message: { message_id: 32, chat: { id: COMMUNITY, type: "supergroup" }, from: gFrom, text: "大家好" } });
  assert.strictEqual(sent(COMMUNITY).length, 0, "没被 @ 不插话");
  await upd({ message: { message_id: 33, chat: { id: COMMUNITY, type: "supergroup" }, from: gFrom, text: "@ctx2026_bot 怎么下载" } });
  assert.match(lastText(COMMUNITY), /官网下载/);

  calls = [];
  await upd({ message: { message_id: 40, chat: { id: COMMUNITY, type: "supergroup" }, from: gFrom, new_chat_members: [{ id: 666, first_name: "Spam" }] } });
  await upd({ message: { message_id: 41, chat: { id: COMMUNITY, type: "supergroup" }, from: { id: 666, first_name: "Spam" }, text: "赚钱 https://t.me/xxx" } });
  assert.ok(calls.some((c) => c.method === "deleteMessage" && c.body.message_id === 41), "新成员链接被删");

  calls = [];
  await upd({ message: { message_id: 50, chat: { id: -100999, type: "supergroup" }, from: gFrom, text: "@ctx2026_bot hi" } });
  assert.strictEqual(calls.length, 0, "未登记的群不理");

  // 群：没 @ 时分类——闲聊不理、成员对话不理、疑似提问让 AI 判断（SKIP 就不说）、产品提问主动答
  {
    const g2 = (message_id: number, who: { id: number; first_name: string }, text: string, extra: Record<string, unknown> = {}) =>
      upd({ message: { message_id, chat: { id: COMMUNITY2, type: "supergroup" }, from: { ...who, language_code: "zh" }, text, ...extra } });
    calls = [];
    const before = llmCalls;
    await g2(60, { id: 701, first_name: "A" }, "哈哈哈哈");
    await g2(61, { id: 702, first_name: "B" }, "早上好");
    await g2(62, { id: 703, first_name: "C" }, "你那个怎么弄的？", { reply_to_message: { message_id: 60, from: { id: 701, is_bot: false } } });
    assert.strictEqual(llmCalls, before, "闲聊 / 成员对话不调 AI");
    llmReply = "SKIP";
    await g2(63, { id: 704, first_name: "D" }, "明天几点开会？");
    assert.strictEqual(llmCalls, before + 1, "疑似提问交给 AI 判断");
    assert.strictEqual(sent(COMMUNITY2).length, 0, "AI 回 SKIP 就不说话");
    llmReply = "点官网下载就行。";
    await g2(64, { id: 705, first_name: "E" }, "智聊怎么下载安装？");
    assert.strictEqual(sent(COMMUNITY2).length, 1, "产品提问主动答");
    assert.strictEqual((sent(COMMUNITY2)[0].body.reply_parameters as { message_id: number }).message_id, 64, "回复到提问那条");
    await g2(65, { id: 706, first_name: "F" }, "会员多少钱？");
    assert.strictEqual(sent(COMMUNITY2).length, 1, "主动答有群冷却，不刷屏");
    await g2(66, { id: 707, first_name: "G" }, "@ctx2026_bot 会员多少钱");
    assert.strictEqual(sent(COMMUNITY2).length, 2, "@ 了照常回答");
  }

  await upd({ my_chat_member: { chat: { id: -100777, type: "supergroup", title: "新群" }, from: gFrom, new_chat_member: { status: "administrator" } } });
  const hub = await H.loadHub();
  const nc = H.findChat(hub, "chatx", -100777);
  assert.ok(nc && nc.enabled === false && nc.botStatus === "administrator", "新群默认停用");

  const { publicHub } = await import("./tg-hub-admin");
  assert.ok(!JSON.stringify(publicHub(hub)).includes("123:TEST"), "对外配置不含 token");
  {
    const { setupChecklist } = await import("./tg-hub-admin");
    const st = (known: number, k: string) => setupChecklist(publicHub(hub), known).find((s) => s.key === k)!;
    assert.ok(st(5, "bot").done && st(5, "known").done && st(5, "hours").done);
    assert.ok(!st(0, "known").done, "问题库全停用 → 未完成");
    assert.ok(!setupChecklist(publicHub({ ...hub, chats: [] }), 5).find((s) => s.key === "support")!.done, "没有客服群 → 未完成");
    assert.match(st(5, "outlet").hint, /停用/, "有新群未启用时提示去启用");
  }

  // 已知问题库：内置默认 + 控制台覆盖 / 自定义 / 停用 / 恢复默认
  {
    const K = await import("./chatx-known-issues");
    await K.refreshKnownIssues();
    assert.strictEqual(K.matchKnownIssue("Windows protected your PC")?.id, "smartscreen", "无文件时用内置");
    assert.strictEqual(K.matchKnownIssue("找不到 MSVCP140.dll"), null);
    await K.saveKnownIssue({ id: "dll-missing", keywords: "msvcp140, 找不到 dll", zh: "安装 VC++ 运行库", en: "", enabled: true });
    await K.saveKnownIssue({ id: "smartscreen", keywords: "蓝色弹窗", zh: "点更多信息", en: "", enabled: true });
    await K.saveKnownIssue({ id: "disk", keywords: "", zh: "", en: "", enabled: false });
    await K.refreshKnownIssues();
    const dll = K.matchKnownIssue("找不到 MSVCP140.dll")!;
    assert.strictEqual(dll.id, "dll-missing", "自定义关键词不区分大小写");
    assert.strictEqual(K.knownIssueText(dll, "en"), "安装 VC++ 运行库", "英文空时回退中文");
    assert.strictEqual(K.matchKnownIssue("弹出蓝色弹窗")?.id, "smartscreen", "内置追加关键词");
    assert.strictEqual(K.matchKnownIssue("smartscreen")?.zh, "点更多信息", "内置文案可覆盖");
    assert.ok(!K.matchKnownIssue("disk full errno 28") || K.matchKnownIssue("disk full errno 28")?.id !== "disk", "停用内置");
    assert.strictEqual(K.matchKnownIssue("a.*b(c"), null, "关键词按字面，不当正则");
    await assert.rejects(K.saveKnownIssue({ id: "Bad Id!", keywords: "x1", zh: "a", en: "", enabled: true }));
    await assert.rejects(K.saveKnownIssue({ id: "nokw", keywords: "", zh: "a", en: "", enabled: true }));
    await K.removeKnownIssue("disk");
    await K.refreshKnownIssues();
    assert.strictEqual(K.matchKnownIssue("disk full")?.id, "disk", "恢复默认");
    const rows = await K.listKnownIssueRows();
    assert.ok(rows.some((r) => r.id === "smartscreen" && r.overridden) && rows.some((r) => r.id === "dll-missing" && !r.builtin));
    await K.removeKnownIssue("dll-missing");
    await K.removeKnownIssue("smartscreen");
  }

  // 工单搜索 + SLA 超时提醒
  {
    const { setSupport } = await import("./tg-hub-admin");
    await setSupport({ hours: "00:00-23:59", tzOffset: 0, slaMin: 15 });
    const t = await T.createTicket({ botId: "chatx", uid: 4242, chatId: 4242, name: "Sla User", username: "slauser", src: "ad_biz_voice01", lang: "zh", kind: "install", fp: "ZZQW-5555-EF56-0001", diag: "K7M2QX", supportBotId: "chatx", supportChatId: "-100555" });
    const all = await T.listTickets();
    assert.deepStrictEqual(T.searchTickets(all, "@slauser").map((x) => x.id), [t.id], "按用户名");
    assert.deepStrictEqual(T.searchTickets(all, "zzqw-5555").map((x) => x.id), [t.id], "按机器码（不区分大小写）");
    assert.deepStrictEqual(T.searchTickets(all, "k7m2qx ad_biz").map((x) => x.id), [t.id], "多词为且");
    assert.ok(T.searchTickets(all, `#${t.id}`).some((x) => x.id === t.id), "按工单号");
    assert.strictEqual(T.searchTickets(all, "slauser", { status: "resolved" }).length, 0, "按状态筛选");
    assert.strictEqual(T.searchTickets(all, "nobody-xyz").length, 0);
    const later = Date.now() + 20 * 60000;
    assert.ok(T.isOverdue(t, 15, later) && !T.isOverdue(t, 15, Date.now()));
    assert.ok(T.searchTickets(all, "slauser", { overdueMin: 15, now: later }).length === 1, "只看超时");

    calls = [];
    const r1 = await S.runSupportSla(later);
    assert.ok(r1.alerted >= 1, "超时提醒已发");
    const alert = calls.find((c) => c.method === "sendMessage" && c.body.chat_id === "-100555" && String(c.body.text).includes(`#${t.id}`));
    assert.ok(alert && String(alert.body.text).includes("⏰"), "提醒发到对应客服群");
    assert.ok((await T.getTicket(t.id))?.slaAlertAt, "落去重标记");
    calls = [];
    const r2 = await S.runSupportSla(later + 60000);
    assert.strictEqual(r2.alerted, 0, "幂等：不重复提醒");
    assert.strictEqual(r2.escalated, 0, "未到升级时间");
    assert.strictEqual(calls.length, 0);
    await assert.rejects(setSupport({ escalateMin: 10 }), "升级时间须大于提醒时间");
    calls = [];
    const r3 = await S.runSupportSla(Date.now() + 50 * 60000);
    assert.ok(r3.escalated >= 1, "超 45 分钟升级管理员");
    const esc = calls.find((c) => c.method === "sendMessage" && String(c.body.chat_id) === "777" && String(c.body.text).includes(`#${t.id}`));
    assert.ok(esc && String(esc.body.text).includes("🚨"), "通知到管理员 chat");
    calls = [];
    const r4 = await S.runSupportSla(Date.now() + 60 * 60000);
    assert.strictEqual(r4.escalated, 0, "升级也只发一次");

    const H = await import("./tg-hub-store");
    assert.deepStrictEqual(H.parseDuty("1-5 09:00-18:00 @alice_1 @bob_22"+"\n"+"6,7 @carol").bad, [], "值班表格式");
    assert.deepStrictEqual(H.parseDuty("周一 @x").bad, ["周一 @x"], "看不懂的行");
    await assert.rejects(setSupport({ duty: "abc" }), /值班表/);
    await assert.rejects(setSupport({ followupMin: -1 }), /追问/);
    const mon10 = Date.parse("2026-09-28T10:00:00Z");
    const duty = { duty: "1-5 09:00-18:00 @alice_1"+"\n"+"6,7 @carol"+"\n"+"1 20:00-02:00 @night", tzOffset: 0 };
    assert.deepStrictEqual(H.dutyNow(duty, mon10), ["alice_1"], "周一白天");
    assert.deepStrictEqual(H.dutyNow(duty, Date.parse("2026-09-27T10:00:00Z")), ["carol"], "周日");
    assert.deepStrictEqual(H.dutyNow(duty, Date.parse("2026-09-28T21:00:00Z")), ["night"], "跨零点时段");
    assert.deepStrictEqual(H.dutyNow({ duty: "", tzOffset: 0 }, mon10), [], "没配值班表");
    await setSupport({ duty: "@alice_1" });
    const t2 = await T.createTicket({ botId: "chatx", uid: 4343, chatId: 4343, name: "Duty User", src: "ad_x", lang: "zh", kind: "human", supportBotId: "chatx", supportChatId: "-100555" });
    calls = [];
    await S.runSupportSla(Date.now() + 20 * 60000);
    const dAlert = calls.find((c) => String(c.body.text).includes("#" + t2.id));
    assert.ok(dAlert && String(dAlert.body.text).includes("值班：@alice_1"), "超时提醒 @ 当班客服");
    calls = [];
    await S.runSupportSla(Date.now() + 50 * 60000);
    const dEsc = calls.find((c) => String(c.body.chat_id) === "777" && String(c.body.text).includes("#" + t2.id));
    assert.ok(dEsc && String(dEsc.body.text).includes("值班：@alice_1"), "升级消息带当班客服");
    await T.updateTicket(t2.id, (x) => void (x.status = "resolved"));

    const F = await import("./chatx-bug-followup");
    await setSupport({ followupMin: 0 });
    await F.recordKnownShown({ botId: "chatx", uid: 5151, chatId: 5151, lang: "zh", known: "network", src: "ad_x" });
    assert.strictEqual((await S.runKnownFollowups(Date.now() + 40 * 60000)).asked, 0, "追问设 0 = 不追问");
    await setSupport({ followupMin: 60 });
    assert.strictEqual((await S.runKnownFollowups(Date.now() + 45 * 60000)).asked, 0, "追问 60 分钟：45 分钟不问");
    assert.strictEqual((await S.runKnownFollowups(Date.now() + 61 * 60000)).asked, 1, "追问 60 分钟：到点问");
    await setSupport({ followupMin: 30 });

    await T.updateTicket(t.id, (x) => void (x.slaAlertAt = undefined));
    await setSupport({ hours: "01:00-01:01", tzOffset: 0 });
    const off = await S.runSupportSla(Date.parse("2026-09-24T12:00:00Z") + 86400_000 * 365);
    assert.ok(off.offHours && off.alerted === 0, "非工作时间不提醒");
    await assert.rejects(setSupport({ slaMin: 0 }));
    await T.updateTicket(t.id, (x) => void (x.status = "resolved"));
  }

  console.log("chatx-support ok");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
