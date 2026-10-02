/**
 * chatx-bot 语音链路冒烟（中继已配）：语音进 → getFile → 下载 → ASR → AI 文字 → TTS → sendVoice；
 * /voice 开关；ASR / TTS 失败只掉语音不掉文字；埋点字段。与 chatx-bot.test.ts 分开跑——那边故意不配中继，
 * 覆盖「语音只提示」的旧路径。运行：npx tsx lib/chatx-bot-voice.test.ts
 */
import assert from "assert";
import fs from "fs";
import os from "os";
import path from "path";

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "chatx-bot-voice-"));
process.env.LEADS_DIR = TMP;
process.env.ANALYTICS_DIR = TMP;
process.env.CHATX_BOT_STARTS_LOG = path.join(TMP, "starts.jsonl");
process.env.CHATX_BOT_REMIND_LOG = path.join(TMP, "reminds.jsonl");
process.env.CHATX_BOT_CTX_FILE = path.join(TMP, "ctx.json");
process.env.CHATX_BOT_HERO_CACHE = path.join(TMP, "hero.json");
process.env.CHATX_BOT_DL_LOG = path.join(TMP, "downloads.jsonl");
process.env.CHATX_BOT_PREFS = path.join(TMP, "prefs.json");
process.env.ADMIN_CHAT_STORE = path.join(TMP, "admins.json");
process.env.CHATX_BOT_TOKEN = "123:TEST";
process.env.TELEGRAM_BOT_TOKEN = "999:MAIN";
process.env.CHAT_USAGE = path.join(TMP, "usage.json");
process.env.CHAT_LOG = path.join(TMP, "chats.jsonl");
process.env.DEEPSEEK_BASE_URL = "https://deepseek.test/v1/chat/completions";
process.env.DEEPSEEK_API_KEY = "sk-test";
process.env.TTS_RELAY_URLS = "http://tts.test";
process.env.ASR_RELAY_URLS = "http://asr.test/v1";
process.env.CHATX_VOICE_COOLDOWN_MS = "0";
const REF = path.join(TMP, "ref.mp3");
fs.writeFileSync(REF, Buffer.from("ID3ref"));
process.env.CHATX_BOT_VOICE_REF = REF;

function makeWav(sampleRate: number, seconds: number): Buffer {
  const frames = Math.round(sampleRate * seconds);
  const data = Buffer.alloc(frames * 2);
  for (let i = 0; i < frames; i++) data.writeInt16LE(Math.round(Math.sin((2 * Math.PI * 440 * i) / sampleRate) * 9000), i * 2);
  const h = Buffer.alloc(44);
  h.write("RIFF", 0, "ascii");
  h.writeUInt32LE(36 + data.length, 4);
  h.write("WAVEfmt ", 8, "ascii");
  h.writeUInt32LE(16, 16);
  h.writeUInt16LE(1, 20);
  h.writeUInt16LE(1, 22);
  h.writeUInt32LE(sampleRate, 24);
  h.writeUInt32LE(sampleRate * 2, 28);
  h.writeUInt16LE(2, 32);
  h.writeUInt16LE(16, 34);
  h.write("data", 36, "ascii");
  h.writeUInt32LE(data.length, 40);
  return Buffer.concat([h, data]);
}

type Part = { name: string; type: string; size: number };
type Call = { method: string; body: Record<string, unknown>; multipart?: { fields: Record<string, string>; file?: Part } };
const calls: Call[] = [];
const relay: string[] = [];
let asrText: string | null = "能接 WhatsApp 吗";
let ttsOk = true;
let fileOk = true;
let llmAnswer = "可以，ChatX 已接入 WhatsApp，AI 会自动回复。👉 下载：https://bd2026.cc/download/chatx?src=x";
let lastSystem = "";

globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
  const url = String(input);
  if (url.startsWith("https://deepseek.test/")) {
    const req = JSON.parse(String(init?.body)) as { messages: { role: string; content: string }[] };
    lastSystem = req.messages.filter((m) => m.role === "system").map((m) => m.content).join("\n");
    return new Response(JSON.stringify({ choices: [{ message: { content: llmAnswer } }] }), { status: 200, headers: { "Content-Type": "application/json" } });
  }
  if (url.startsWith("http://asr.test/")) {
    relay.push("asr");
    return asrText === null ? new Response("bad", { status: 500 }) : new Response(JSON.stringify({ text: asrText }), { status: 200 });
  }
  if (url.startsWith("http://tts.test/")) {
    relay.push("tts");
    return ttsOk
      ? new Response(JSON.stringify({ ok: true, audio_base64: makeWav(16000, 1.2).toString("base64") }), { status: 200 })
      : new Response(JSON.stringify({ detail: "gpu busy" }), { status: 500 });
  }
  if (url.startsWith("https://api.telegram.org/file/bot123:TEST/")) {
    calls.push({ method: "file:" + url.split("/").slice(-2).join("/"), body: {} });
    return fileOk ? new Response(new Uint8Array([0x4f, 0x67, 0x67, 0x53, 9, 9, 9]), { status: 200 }) : new Response("nf", { status: 404 });
  }
  const method = url.split("/").pop() ?? "";
  if (init?.body instanceof FormData) {
    const fields: Record<string, string> = {};
    let file: Part | undefined;
    for (const [k, v] of init.body.entries()) {
      if (typeof v === "string") fields[k] = v;
      else file = { name: v.name, type: v.type, size: v.size };
    }
    calls.push({ method, body: fields, multipart: { fields, file } });
    return new Response(JSON.stringify({ ok: true, result: { message_id: calls.length } }), { status: 200 });
  }
  const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {};
  calls.push({ method, body });
  let result: unknown = { message_id: calls.length };
  if (method === "getFile") result = fileOk ? { file_id: body.file_id, file_path: "voice/file_7.oga", file_size: 7 } : undefined;
  return new Response(JSON.stringify({ ok: method === "getFile" ? fileOk : true, result }), { status: 200, headers: { "Content-Type": "application/json" } });
}) as typeof fetch;

const events = (): Array<{ event: string; props: Record<string, unknown> }> =>
  fs
    .readFileSync(path.join(TMP, "events.jsonl"), "utf-8")
    .split("\n")
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l));
/** 等落盘 + 过 800ms 同会话刷屏限流。 */
const settle = () => new Promise((r) => setTimeout(r, 850));

async function main() {
  const B = await import("./chatx-bot");
  const V = await import("./chatx-voice");
  const from = { id: 8080, username: "voicer", first_name: "V" };
  await B.handleStart(8080, from, "/start ad_voice01", "zh");
  await settle();

  // ① 语音进（默认用户）：getFile → 下载 → ASR → AI 文字（带「🎙 听到的」引用 + 键盘） → TTS → sendVoice
  calls.length = 0;
  relay.length = 0;
  await B.handleVoiceMessage(8080, from, { file_id: "AwAC_voice", duration: 4, mime_type: "audio/ogg" }, "zh");
  await settle();
  const seq = calls.map((c) => c.method);
  assert.deepStrictEqual(relay, ["asr", "tts"], "ASR 先、TTS 后");
  assert.ok(seq.indexOf("getFile") < seq.indexOf("file:voice/file_7.oga"), "getFile 后再下载文件");
  const textMsg = calls.find((c) => c.method === "sendMessage")!;
  assert.ok(textMsg, "先发文字");
  assert.match(String(textMsg.body.text), /^🎙 能接 WhatsApp 吗\n\n可以，ChatX 已接入 WhatsApp/, "文字回复引用转写原文，正文去链接");
  assert.ok(!String(textMsg.body.text).includes("https://"), "链接进按钮不进正文");
  assert.ok(textMsg.body.reply_markup, "带键盘");
  const voiceMsg = calls.find((c) => c.method === "sendVoice")!;
  assert.ok(voiceMsg, "再发语音");
  assert.ok(seq.indexOf("sendMessage") < seq.indexOf("sendVoice"), "文字先于语音（TTS 慢也不拖文字）");
  assert.strictEqual(voiceMsg.multipart!.fields.chat_id, "8080");
  assert.strictEqual(voiceMsg.multipart!.fields.duration, "1", "1.2s 四舍五入");
  assert.strictEqual(voiceMsg.multipart!.file!.type, "audio/mpeg");
  assert.ok(voiceMsg.multipart!.file!.size > 500, "MP3 有内容");
  assert.ok(calls.some((c) => c.method === "sendChatAction" && c.body.action === "record_voice"), "合成期间显示 record_voice");
  let ev = events();
  const vin = ev.filter((e) => e.event === "chatx_bot_voice_in").pop()!;
  assert.ok(vin && vin.props.ok === true && vin.props.src === "ad_voice01" && vin.props.uid === 8080 && vin.props.sec === 4 && vin.props.chars === "能接 WhatsApp 吗".length, `voice_in 埋点 ${JSON.stringify(vin?.props)}`);
  const vout = ev.filter((e) => e.event === "chatx_bot_voice_out").pop()!;
  assert.ok(vout && vout.props.ok === true && vout.props.persona === "xiaojie" && vout.props.sec === 1 && typeof vout.props.chars === "number", `voice_out 埋点 ${JSON.stringify(vout?.props)}`);
  const ai = ev.filter((e) => e.event === "chatx_bot_ai").pop()!;
  assert.strictEqual(ai.props.via, "voice", "AI 事件标记 via=voice");

  // ② 纯文字用户发文字：不附语音（默认行为不变）
  calls.length = 0;
  relay.length = 0;
  await B.handleFreeText(8080, from, "价格多少", "zh");
  await settle();
  assert.ok(calls.some((c) => c.method === "sendMessage"));
  assert.ok(!calls.some((c) => c.method === "sendVoice") && !relay.includes("tts"), "文字进 → 只回文字");

  // ③ /voice on → 文字也附语音；/voice off → 连语音进也只回文字；/voice 切换
  calls.length = 0;
  await B.handleOther(8080, from, "/voice on", "zh");
  assert.match(String(calls.find((c) => c.method === "sendMessage")!.body.text), /语音回复已开/);
  calls.length = 0;
  relay.length = 0;
  await B.handleFreeText(8080, from, "能接微信吗", "zh");
  await settle();
  assert.ok(calls.some((c) => c.method === "sendVoice") && relay.includes("tts"), "/voice on 后文字也回语音");
  calls.length = 0;
  await B.handleOther(8080, from, "/voice off", "zh");
  assert.match(String(calls.find((c) => c.method === "sendMessage")!.body.text), /语音回复已关/);
  calls.length = 0;
  relay.length = 0;
  await B.handleVoiceMessage(8080, from, { file_id: "AwAC_voice2", duration: 3 }, "zh");
  await settle();
  assert.deepStrictEqual(relay, ["asr"], "关掉后语音进也只转写不合成");
  assert.ok(calls.some((c) => c.method === "sendMessage") && !calls.some((c) => c.method === "sendVoice"));
  calls.length = 0;
  await B.handleOther(8080, from, "/voice", "zh");
  assert.match(String(calls.find((c) => c.method === "sendMessage")!.body.text), /语音回复已开/, "不带参数 = 切换");
  await B.handleOther(8080, from, "/voice", "zh");
  ev = events();
  assert.deepStrictEqual(
    ev.filter((e) => e.event === "chatx_bot_cmd" && e.props.cmd === "voice").map((e) => e.props.on),
    [true, false, true, false],
    "/voice 埋点记录开关"
  );

  // ④ 太长的语音：不下载、不转写，一句话提示 + 埋点 why=too_long
  calls.length = 0;
  relay.length = 0;
  await B.handleVoiceMessage(8080, from, { file_id: "AwAC_long", duration: V.VOICE_IN_MAX_SEC + 1 }, "zh");
  await settle();
  assert.strictEqual(relay.length, 0);
  assert.ok(!calls.some((c) => c.method === "getFile"));
  assert.match(String(calls.find((c) => c.method === "sendMessage")!.body.text), /有点长/);
  assert.strictEqual(events().filter((e) => e.event === "chatx_bot_voice_in").pop()!.props.why, "too_long");

  // ⑤ ASR 挂：提示「没听清」，不调 AI
  asrText = null;
  calls.length = 0;
  relay.length = 0;
  await B.handleVoiceMessage(8080, from, { file_id: "AwAC_bad", duration: 2 }, "zh");
  await settle();
  assert.match(String(calls.find((c) => c.method === "sendMessage")!.body.text), /没听清/);
  assert.ok(!calls.some((c) => c.method === "sendVoice"));
  assert.strictEqual(events().filter((e) => e.event === "chatx_bot_voice_in").pop()!.props.why, "asr_failed");
  asrText = "hello";

  // ⑥ getFile 失败：同样「没听清」，不打 ASR
  fileOk = false;
  calls.length = 0;
  relay.length = 0;
  await B.handleVoiceMessage(8080, from, { file_id: "AwAC_gone", duration: 2 }, "en");
  await settle();
  assert.strictEqual(relay.length, 0, "文件拿不到就不打 ASR");
  assert.match(String(calls.find((c) => c.method === "sendMessage")!.body.text), /couldn't make that out/);
  fileOk = true;

  // ⑦ TTS 挂（另一个没设过 /voice 的用户，语音进默认回语音）：文字照发，语音无，埋点 why=tts_failed
  const from2 = { id: 8081, first_name: "W" };
  ttsOk = false;
  calls.length = 0;
  relay.length = 0;
  await B.handleVoiceMessage(8081, from2, { file_id: "AwAC_v3", duration: 2 }, "zh");
  await settle();
  assert.ok(calls.some((c) => c.method === "sendMessage" && /^🎙 hello/.test(String(c.body.text))), "文字回答仍在");
  assert.ok(!calls.some((c) => c.method === "sendVoice"), "TTS 失败不发语音");
  assert.strictEqual(events().filter((e) => e.event === "chatx_bot_voice_out").pop()!.props.why, "tts_failed");
  ttsOk = true;

  // ⑧ 回答过长（> VOICE_TEXT_MAX）不合成，埋点 why=too_long；配额记录不占 GPU
  llmAnswer = "很长".repeat(200);
  calls.length = 0;
  relay.length = 0;
  await B.handleVoiceMessage(8081, from2, { file_id: "AwAC_v4", duration: 2 }, "zh");
  await settle();
  assert.deepStrictEqual(relay, ["asr"], "太长的答案不打 TTS");
  assert.strictEqual(events().filter((e) => e.event === "chatx_bot_voice_out").pop()!.props.why, "too_long");
  llmAnswer = "可以，ChatX 已接入 WhatsApp，AI 会自动回复。";

  // ⑨ 人设：/persona 菜单 → 切恋爱陪聊 → 文字消息也默认带语音、system 叠人设层但保留小界 + 下载链 → 按钮切回小界恢复默认
  const from3 = { id: 8082, first_name: "P" };
  await B.handleStart(8082, from3, "/start ad_lover01", "zh");
  await settle();
  calls.length = 0;
  await B.handleOther(8082, from3, "/persona", "zh");
  const menu = calls.find((c) => c.method === "sendMessage")!;
  assert.match(String(menu.body.text), /当前人设：<b>🤖 小界/, "菜单显示当前人设");
  const pkb = (menu.body.reply_markup as { inline_keyboard: { text: string; callback_data: string }[][] }).inline_keyboard;
  assert.deepStrictEqual(pkb.map((r) => r.map((b) => b.callback_data)), [["cx_persona:xiaojie"], ["cx_persona:lover", "cx_persona:sales"]], "默认独占首行，两个演示人设同行");
  assert.ok(pkb[0][0].text.startsWith("✅"), "当前项打勾");
  assert.match(String(menu.body.text), /💼 销售跟进/, "菜单列出销售跟进");

  calls.length = 0;
  await B.handleOther(8082, from3, "/persona 恋爱", "zh");
  assert.strictEqual((await (await import("./chatx-prefs")).getPrefs(8082)).persona, "lover");
  const sw = calls.find((c) => c.method === "sendMessage")!;
  assert.match(String(sw.body.text), /💗 好啦/, "切换确认用新人设语气");
  assert.match(String(sw.body.text), /\/voice 关掉/, "提示语音默认开");
  const pev = events().filter((e) => e.event === "chatx_bot_cmd" && e.props.cmd === "persona").pop()!;
  assert.deepStrictEqual({ p: pev.props.persona, f: pev.props.from, src: pev.props.src }, { p: "lover", f: "xiaojie", src: "ad_lover01" });

  await settle();
  calls.length = 0;
  relay.length = 0;
  await B.handleFreeText(8082, from3, "今天好累", "zh");
  await settle();
  assert.ok(/【人设：恋爱陪聊】/.test(lastSystem), "system 叠人设层");
  assert.ok(/你是小界/.test(lastSystem) && /【当前场景】/.test(lastSystem) && /download\/chatx\?[^\s]*src=ad_lover01/.test(lastSystem), "仍保留小界人设 + ChatX 场景 + 带 src 下载链");
  assert.ok(/不涉色情/.test(lastSystem), "带底线");
  assert.deepStrictEqual(relay, ["tts"], "恋爱陪聊文字消息也默认回语音");
  assert.ok(calls.some((c) => c.method === "sendVoice"));
  const aiEv = events().filter((e) => e.event === "chatx_bot_ai").pop()!;
  assert.strictEqual(aiEv.props.persona, "lover");
  assert.strictEqual(events().filter((e) => e.event === "chatx_bot_voice_out").pop()!.props.persona, "lover");

  calls.length = 0;
  await B.handleCallback(8082, "cq9", "cx_persona:xiaojie", from3, "zh");
  assert.strictEqual((await (await import("./chatx-prefs")).getPrefs(8082)).persona, "xiaojie");
  assert.match(String(calls.find((c) => c.method === "sendMessage")!.body.text), /已切回小界/);
  await settle();
  calls.length = 0;
  relay.length = 0;
  await B.handleFreeText(8082, from3, "怎么下载", "zh");
  await settle();
  assert.ok(!/恋爱陪聊/.test(lastSystem), "切回后无人设层");
  assert.deepStrictEqual(relay, [], "小界文字消息不带语音（默认行为不变）");
  assert.strictEqual(events().filter((e) => e.event === "chatx_bot_ai").pop()!.props.persona, "xiaojie");

  // 用户显式 /voice off 后切恋爱陪聊：尊重显式设置，不回语音，确认语也不提语音
  await B.handleOther(8082, from3, "/voice off", "zh");
  calls.length = 0;
  await B.handleOther(8082, from3, "/persona lover", "zh");
  assert.ok(!/\/voice 关掉/.test(String(calls.find((c) => c.method === "sendMessage")!.body.text)), "已关语音时不提");
  await settle();
  relay.length = 0;
  await B.handleFreeText(8082, from3, "晚安", "zh");
  await settle();
  assert.deepStrictEqual(relay, [], "显式关语音优先于人设默认");

  // ⑩ 销售跟进：别名 / 按钮都能切；确认语带第一个跟进问题；system 叠销售层但保留小界 + 下载链；默认不带语音（同小界）；切回后无销售层
  const from4 = { id: 8083, first_name: "S" };
  await B.handleStart(8083, from4, "/start ad_sales01", "zh");
  await settle();
  const P = await import("./chatx-persona");
  for (const a of ["sales", "sale", "销售", "跟进", "销售跟进", "3", "follow-up"]) assert.strictEqual(P.parsePersona(a), "sales", `别名 ${a}`);
  assert.strictEqual(P.parsePersona("销售员工"), null, "不模糊匹配");
  calls.length = 0;
  await B.handleOther(8083, from4, "/persona 销售", "zh");
  assert.strictEqual((await (await import("./chatx-prefs")).getPrefs(8083)).persona, "sales");
  const sws = String(calls.find((c) => c.method === "sendMessage")!.body.text);
  assert.match(sws, /💼 好/, "销售语气确认");
  assert.match(sws, /哪个平台接客户/, "确认语自带第一个跟进问题");
  assert.ok(!/\/voice 关掉/.test(sws), "销售人设默认不开语音，不提关闭");
  const sev = events().filter((e) => e.event === "chatx_bot_cmd" && e.props.cmd === "persona").pop()!;
  assert.deepStrictEqual({ p: sev.props.persona, f: sev.props.from, src: sev.props.src }, { p: "sales", f: "xiaojie", src: "ad_sales01" });
  await settle();
  calls.length = 0;
  relay.length = 0;
  await B.handleFreeText(8083, from4, "我做跨境电商，客户都在 WhatsApp", "zh");
  await settle();
  assert.ok(/【人设：销售跟进】/.test(lastSystem), "system 叠销售层");
  assert.ok(/只带 1 个跟进问题/.test(lastSystem) && /不编、不承诺/.test(lastSystem), "销售层含跟进纪律 + 不编价格");
  assert.ok(/你是小界/.test(lastSystem) && /download\/chatx\?[^\s]*src=ad_sales01/.test(lastSystem), "仍保留小界 + 带 src 下载链");
  assert.ok(!/恋爱陪聊】/.test(lastSystem), "不混入恋爱层");
  assert.deepStrictEqual(relay, [], "销售人设文字消息默认不回语音");
  assert.strictEqual(events().filter((e) => e.event === "chatx_bot_ai").pop()!.props.persona, "sales");
  calls.length = 0;
  await B.handleCallback(8083, "cq10", "cx_persona:xiaojie", from4, "zh");
  assert.strictEqual((await (await import("./chatx-prefs")).getPrefs(8083)).persona, "xiaojie");
  await settle();
  await B.handleFreeText(8083, from4, "怎么下载", "zh");
  await settle();
  assert.ok(!/销售跟进】/.test(lastSystem), "切回后无销售层");
  // 早报开场白：销售人设有自己的一句，小界仍无
  assert.match(P.pushOpener("sales", "zh")!, /ChatX 装上了吗/);
  assert.match(P.pushOpener("sales", "en")!, /installed yet/);
  assert.strictEqual(P.pushOpener("xiaojie", "zh"), undefined);
  assert.strictEqual(P.pushOpener(undefined, "zh"), undefined);

  console.log("chatx-bot-voice smoke OK");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
