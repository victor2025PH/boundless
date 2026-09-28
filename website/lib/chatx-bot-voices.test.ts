/**
 * chatx-bot 音色选择 + 克隆自己声音的对话链路：/voices 菜单、选库音色（试听原声）、克隆授权 → 样本体检 → 明确选人设才生效、删除。
 * /voice 开关；ASR / TTS 失败只掉语音不掉文字；埋点字段。与 chatx-bot.test.ts 分开跑——那边故意不配中继，
 * 运行：npx tsx lib/chatx-bot-voices.test.ts
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
process.env.CHATX_VOICEPACK_DIR = path.join(TMP, "pack");
process.env.CHATX_USER_VOICE_DIR = path.join(TMP, "users");
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
const asrText: string | null = "能接 WhatsApp 吗";
const ttsOk = true;
const fileOk = true;
let llmAnswer = "可以，ChatX 已接入 WhatsApp。";
let lastSystem = "";
let fileBody: Buffer = Buffer.from("OggS");
const ttsRefs: string[] = [];
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
    ttsRefs.push((JSON.parse(String(init?.body)) as { reference_audio_b64: string }).reference_audio_b64);
    return ttsOk
      ? new Response(JSON.stringify({ ok: true, audio_base64: makeWav(16000, 1.2).toString("base64") }), { status: 200 })
      : new Response(JSON.stringify({ detail: "gpu busy" }), { status: 500 });
  }
  if (url.startsWith("https://api.telegram.org/file/bot123:TEST/")) {
    calls.push({ method: "file:" + url.split("/").slice(-2).join("/"), body: {} });
    return fileOk ? new Response(new Uint8Array(fileBody), { status: 200 }) : new Response("nf", { status: 404 });
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


type Kb = { inline_keyboard: Array<Array<{ text: string; callback_data?: string }>> };
const lastMsg = () => [...calls].reverse().find((c) => c.method === "sendMessage")!;
const btns = (c: Call) => ((c.body.reply_markup as Kb | undefined)?.inline_keyboard ?? []).flat();

function speechWav(sr: number, sec: number, amp = 0.3): Buffer {
  const n = Math.round(sr * sec);
  const data = Buffer.alloc(n * 2);
  for (let i = 0; i < n; i++) data.writeInt16LE(Math.round(Math.sin((2 * Math.PI * 220 * i) / sr) * amp * (0.6 + 0.4 * Math.sin(i / 900)) * 32767), i * 2);
  const h = makeWav(sr, 0).subarray(0, 44);
  h.writeUInt32LE(36 + data.length, 4);
  h.writeUInt32LE(data.length, 40);
  return Buffer.concat([h, data]);
}

async function main() {
  const B = await import("./chatx-bot");
  const P = await import("./chatx-voicepack");
  const { getPrefs } = await import("./chatx-prefs");
  const uid = 9090;
  const from = { id: uid, username: "cloner", first_name: "C" };
  fs.mkdirSync(P.VOICEPACK_DIR, { recursive: true });
  fs.writeFileSync(P.voicepackPath("SSB0016"), speechWav(22050, 10));
  fs.writeFileSync(P.voicepackPath("SSB0710"), speechWav(22050, 10));
  await B.handleStart(uid, from, "/start ad_voices01", "zh");
  await settle();

  // ① /voices 菜单：只列已落盘的库音色，默认打勾，恋爱人设的推荐 ⭐；/voice 仍是开关（不被 /voices 吞）
  calls.length = 0;
  await B.handleOther(uid, from, "/voices lover", "zh");
  let m = lastMsg();
  assert.match(String(m.body.text), /恋爱陪聊/);
  let b = btns(m);
  assert.ok(b.some((x) => x.callback_data === "cx_vset:lover:SSB0016" && x.text.startsWith("⭐")), "推荐标星");
  assert.ok(!b.some((x) => x.callback_data?.includes("SSB0966")), "没落盘的不列");
  assert.ok(b.some((x) => x.callback_data === "cx_vset:lover:default" && x.text.startsWith("✅")), "默认打勾");
  assert.ok(b.some((x) => x.callback_data === "cx_vclone"));
  await settle();

  // ② 选库音色：写 prefs（只改该人设）+ 试听原声（sendVoice，不走 TTS）
  calls.length = 0;
  relay.length = 0;
  await B.handleCallback(uid, "cq1", "cx_vset:lover:SSB0016", from, "zh");
  let prefs = await getPrefs(uid);
  assert.deepStrictEqual(prefs.voices, { lover: "SSB0016" });
  assert.ok(calls.some((c) => c.method === "sendVoice"), "试听");
  assert.deepStrictEqual(relay, [], "试听不耗 GPU");
  await B.handleCallback(uid, "cq2", "cx_vset:lover:../../x", from, "zh");
  assert.deepStrictEqual((await getPrefs(uid)).voices, { lover: "SSB0016" }, "非法值忽略");
  await settle();

  // ③ 恋爱人设回话用选中的音色，小界不受影响
  await B.handlePersona(uid, from, "lover", "zh");
  await settle();
  ttsRefs.length = 0;
  await B.handleFreeText(uid, from, "你好", "zh");
  await settle();
  assert.strictEqual(ttsRefs[0], fs.readFileSync(P.voicepackPath("SSB0016")).toString("base64"), "lover 用 SSB0016");
  await settle();

  // ④ 未授权时语音照常走 ASR 对话（不当克隆样本）
  relay.length = 0;
  fileBody = speechWav(48000, 9);
  await B.handleVoiceMessage(uid, from, { file_id: "v0", duration: 9 }, "zh");
  await settle();
  assert.strictEqual(relay[0], "asr", "未授权 → 普通语音对话");
  assert.ok(!(await getPrefs(uid)).myVoice);

  // ⑤ 克隆：说明 + 授权按钮 → 同意 → 过短样本拒收（保持窗口）→ 合格样本登记，不自动替换任何人设
  calls.length = 0;
  await B.handleOther(uid, from, "/clone", "zh");
  m = lastMsg();
  assert.match(String(m.body.text), /本人/);
  assert.ok(btns(m).some((x) => x.callback_data === "cx_vconsent"));
  await B.handleCallback(uid, "cq3", "cx_vconsent", from, "zh");
  assert.ok((await getPrefs(uid)).cloneArmedAt);
  await settle();
  relay.length = 0;
  fileBody = speechWav(48000, 2);
  await B.handleVoiceMessage(uid, from, { file_id: "v1", duration: 2 }, "zh");
  assert.match(String(lastMsg().body.text), /录音过短/);
  assert.deepStrictEqual(relay, [], "样本不送 ASR");
  prefs = await getPrefs(uid);
  assert.ok(!prefs.myVoice && prefs.cloneArmedAt, "拒收后仍可重录");
  await settle();
  fileBody = speechWav(48000, 9);
  calls.length = 0;
  await B.handleVoiceMessage(uid, from, { file_id: "v2", duration: 9 }, "zh");
  prefs = await getPrefs(uid);
  assert.ok(prefs.myVoice?.owner_consent && fs.existsSync(prefs.myVoice.path), "登记成功");
  assert.ok(!prefs.cloneArmedAt, "窗口关闭");
  assert.deepStrictEqual(prefs.voices, { lover: "SSB0016" }, "不自动替换");
  const ask = calls.find((c) => c.method === "sendMessage" && /哪个人设/.test(String(c.body.text)))!;
  assert.ok(btns(ask).some((x) => x.callback_data === "cx_vset:sales:mine"));
  assert.ok(calls.some((c) => c.method === "sendVoice"), "用克隆音示范一句");
  await settle();

  // ⑥ 明确选用 → 该人设用我的声音
  await B.handleCallback(uid, "cq4", "cx_vset:sales:mine", from, "zh");
  prefs = await getPrefs(uid);
  assert.deepStrictEqual(prefs.voices, { lover: "SSB0016", sales: "mine" });
  const mine = prefs.myVoice!.path;
  await settle();

  // ⑦ 删除：文件删掉、用到 mine 的人设回默认、其它选择保留
  await B.handleCallback(uid, "cq5", "cx_vdel", from, "zh");
  prefs = await getPrefs(uid);
  assert.ok(!prefs.myVoice && !fs.existsSync(mine));
  assert.deepStrictEqual(prefs.voices, { lover: "SSB0016" });

  // ⑧ 本条会配语音：系统提示声明能发语音；模型说的"发不了语音"从文字里去掉，语音照发
  llmAnswer = "语音这块我这边暂时发不了，不过 ChatX 本身支持人设语音聊天。";
  await B.handlePersona(uid, from, "lover", "zh");
  await settle();
  calls.length = 0;
  relay.length = 0;
  await B.handleFreeText(uid, from, "你能发语音吗", "zh");
  await settle();
  assert.match(lastSystem, /你能发语音/);
  const said = calls.filter((c) => c.method === "sendMessage").map((c) => String(c.body.text)).join("\n");
  assert.ok(!/发不了/.test(said) && /ChatX 本身支持人设语音聊天/.test(said), said);
  assert.ok((relay as string[]).includes("tts") && calls.some((c) => c.method === "sendVoice"), "语音照发");

  // ⑨ 小界默认不配语音：提示为"关着、可 /voice 打开"，不删文字；明确要语音 → 本条也配语音
  await B.handlePersona(uid, from, "xiaojie", "zh");
  await settle();
  calls.length = 0;
  relay.length = 0;
  await B.handleFreeText(uid, from, "介绍一下", "zh");
  await settle();
  assert.match(lastSystem, /关着语音回复/);
  assert.ok(!(relay as string[]).includes("tts"), "没要语音不合成");
  assert.ok(calls.some((c) => c.method === "sendMessage" && /发不了/.test(String(c.body.text))), "不配语音时不改写模型原文");
  relay.length = 0;
  await B.handleFreeText(uid, from, "给我发条语音听听", "zh");
  await settle();
  assert.match(lastSystem, /你能发语音/);
  assert.ok((relay as string[]).includes("tts"), "明确要语音 → 配语音");
  llmAnswer = "可以，ChatX 已接入 WhatsApp。";

  const ev = events();
  assert.ok(ev.some((e) => e.event === "chatx_bot_voice_pick" && e.props.voice === "SSB0016" && e.props.preview === true));
  const steps = ev.filter((e) => e.event === "chatx_bot_voice_clone").map((e) => `${e.props.step}${e.props.ok === undefined ? "" : ":" + e.props.ok}`);
  assert.deepStrictEqual(steps, ["start", "consent", "sample:false", "sample:true", "delete"]);
  assert.ok(ev.some((e) => e.event === "chatx_bot_voice_out" && e.props.ref === "lib" && e.props.voice === "SSB0016"));
  console.log("chatx-bot-voices OK");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
