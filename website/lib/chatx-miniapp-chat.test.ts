/**
 * ChatX 小程序 AI 问答冒烟：npx tsx lib/chatx-miniapp-chat.test.ts（mock DeepSeek，不出网）。
 * 断言 /api/chat scene=chatx 与 @ctx2026_bot 同一条链：小界人设 + 官网知识库 + ChatX 场景提示 + 会话历史；
 * AI 不可用时只回知识库命中、没命中就 503（不给通用兜底菜单）。
 */
import assert from "assert";
import fs from "fs";
import os from "os";
import path from "path";

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "chatx-miniapp-"));
process.env.LEADS_DIR = TMP;
process.env.ANALYTICS_DIR = TMP;
process.env.CHAT_USAGE = path.join(TMP, "usage.json");
process.env.CHAT_LOG = path.join(TMP, "chats.jsonl");
process.env.DEEPSEEK_API_KEY = "test-key";
process.env.DEEPSEEK_BASE_URL = "https://deepseek.test/v1/chat/completions";

let llmChunks: string[] | null = null;
const llmRequests: Array<Record<string, unknown>> = [];

globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
  const url = String(input);
  assert.ok(url.startsWith("https://deepseek.test/"), `unexpected fetch ${url}`);
  llmRequests.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
  if (llmChunks === null) return new Response("upstream down", { status: 503 });
  const enc = new TextEncoder();
  const chunks = llmChunks;
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      for (const piece of chunks) c.enqueue(enc.encode(`data: ${JSON.stringify({ choices: [{ delta: { content: piece } }] })}\n\n`));
      c.enqueue(enc.encode("data: [DONE]\n\n"));
      c.close();
    },
  });
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}) as typeof fetch;

function post(body: unknown, ip = "203.0.113.7") {
  return new Request("http://localhost/api/chat", {
    method: "POST",
    headers: { "content-type": "application/json", "x-forwarded-for": ip },
    body: JSON.stringify(body),
  });
}

async function main() {
  const { POST } = await import("../app/api/chat/route");
  type Msg = { role: string; content: string };

  // 1) AI 正常：流式回传 + system = 小界人设 + 知识库 + ChatX 小程序场景（下载/教程链带 src）+ 历史
  llmChunks = ["可以的，", "装好 ChatX 后客户消息 AI 会自动回。"];
  llmRequests.length = 0;
  const res = await POST(
    post({
      message: "能接 WhatsApp 吗",
      lang: "zh",
      scene: "chatx",
      src: "ad_mini_07",
      history: [{ role: "assistant", content: "你好，我是小界。" }],
    }) as never
  );
  assert.strictEqual(res.status, 200);
  assert.strictEqual(res.headers.get("X-Chat-Source"), "ai");
  assert.strictEqual(await res.text(), "可以的，装好 ChatX 后客户消息 AI 会自动回。");
  const msgs = llmRequests[0].messages as Msg[];
  const sys = msgs[0];
  assert.strictEqual(sys.role, "system");
  assert.ok(/你是小界/.test(sys.content), "小界人设");
  assert.ok(/资料：/.test(sys.content), "官网知识库上下文");
  assert.ok(/Telegram 小程序里/.test(sys.content), "ChatX 小程序场景提示");
  assert.ok(/download\/chatx\?[^\s]*src=ad_mini_07/.test(sys.content), "下载链带 src");
  assert.ok(/chatx\/tutorials\?[^\s]*src=ad_mini_07/.test(sys.content), "教程链带 src");
  assert.ok(/utm_medium=chatx_miniapp/.test(sys.content) && !/utm_medium=chatx_bot/.test(sys.content), "小程序场景链接 medium=chatx_miniapp");
  assert.ok(msgs.some((m) => m.role === "assistant" && m.content === "你好，我是小界。"), "小界问候作为历史上下文");
  assert.strictEqual(msgs[msgs.length - 1].content, "能接 WhatsApp 吗");
  assert.ok((llmRequests[0] as { stream?: boolean }).stream === true, "小程序走流式");

  // 2) 非 chatx 场景不叠 ChatX 提示（官网通用 AI 不受影响）
  llmRequests.length = 0;
  await (await POST(post({ message: "换脸怎么收费", lang: "zh" }) as never)).text();
  const sys2 = (llmRequests[0].messages as Msg[])[0].content;
  assert.ok(/你是小界/.test(sys2) && !/Telegram 小程序里/.test(sys2), "通用场景仍是小界，无 ChatX 场景提示");

  // 3) AI 不可用 + 知识库命中 → 回知识库（小界资料），标 kb
  llmChunks = null;
  const kbRes = await POST(post({ message: "价格", lang: "zh", scene: "chatx", src: "ad_mini_07" }) as never);
  assert.strictEqual(kbRes.status, 200);
  assert.strictEqual(kbRes.headers.get("X-Chat-Source"), "kb");
  assert.ok((await kbRes.text()).length > 20, "知识库命中有内容");

  // 4) AI 不可用 + 知识库未命中 → 503，不给「点下方按钮打开 Mini App」通用兜底
  const none = await POST(post({ message: "呜啦啦啦", lang: "zh", scene: "chatx", src: "ad_mini_07" }) as never);
  assert.strictEqual(none.status, 503);
  assert.deepStrictEqual(await none.json(), { ok: false, error: "ai_unavailable" });

  // 5) 通用场景同样情况仍回原兜底（行为不变）
  const generic = await POST(post({ message: "呜啦啦啦", lang: "zh" }) as never);
  assert.strictEqual(generic.status, 200);
  assert.ok(/没完全理解|试试发/.test(await generic.text()), "通用场景保留原兜底");

  // 5b) 韩/日语境：通用场景给母语兜底话术，ChatX 场景同样不给（503）
  const koGeneric = await POST(post({ message: "안녕하세요 뭐든지", lang: "ko" }) as never);
  assert.strictEqual(koGeneric.status, 200);
  assert.ok(/지금 접속이 많아/.test(await koGeneric.text()), "通用场景韩语兜底不变");
  const koChatx = await POST(post({ message: "안녕하세요 뭐든지", lang: "ko", scene: "chatx", src: "ad_mini_07" }) as never);
  assert.strictEqual(koChatx.status, 503, "ChatX 场景不用韩语兜底话术");

  // 6) 日志 source 区分小程序（logChat 异步落盘）
  await new Promise((r) => setTimeout(r, 300));
  const log = fs.readFileSync(process.env.CHAT_LOG!, "utf-8").trim().split("\n").map((l) => JSON.parse(l) as { source: string });
  const sources = log.map((r) => r.source);
  assert.ok(sources.includes("chatx_miniapp_ai") && sources.includes("chatx_miniapp_kb") && sources.includes("chatx_miniapp_unavailable"), `sources=${sources.join(",")}`);

  console.log("chatx-miniapp-chat smoke OK");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
