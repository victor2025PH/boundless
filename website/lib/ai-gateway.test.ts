/**
 * 纯函数冒烟：npx tsx lib/ai-gateway.test.ts（不依赖 Next runtime）。
 * env 必须在动态 import 前设好（模块常量在 import 时求值）。
 */
import assert from "assert";
import fs from "fs";
import os from "os";
import path from "path";

process.env.DEEPSEEK_API_KEY = "sk-test-for-unit";
process.env.AI_GATEWAY_SECRET = "unit-test-secret";
process.env.AI_GATEWAY_DAILY_CHARS = "100";
process.env.AI_GATEWAY_GLOBAL_DAILY_CHARS = "150";
process.env.AI_GATEWAY_MAX_TOKENS = "2048";
// 刻意保留退役名：白名单条目须被归一到 deepseek-flash（env 残留旧名不放进白名单）
process.env.AI_GATEWAY_MODELS = "deepseek-chat,deepseek-v4-flash,deepseek-v4-pro";
process.env.DEEPSEEK_BACKUP_API_KEY = "sk-backup-unit";
process.env.DEEPSEEK_BACKUP_BASE_URL = "https://api.siliconflow.cn/v1/chat/completions";
process.env.DEEPSEEK_BACKUP_MODEL = "deepseek-ai/DeepSeek-V4-Flash";
process.env.AI_GATEWAY_QUOTA_DB = path.join(
  fs.mkdtempSync(path.join(os.tmpdir(), "gwtest-")),
  "quota.db"
);
// 刻意**只**设识图中继、不设 EMBED_RELAY_URLS：嵌入必须自动沿用它
// （bge-m3 与 VLM 同住 Ollama，复用同一条隧道端口 → 上线不必新增 env/隧道）。
process.env.VISION_RELAY_URLS = "http://127.0.0.1:18411/v1,http://127.0.0.1:18412/v1";

async function main() {
  const gw = await import("./ai-gateway");

  // ── 指纹归一 + 令牌往返 ──
  assert.equal(gw.normalizeFingerprint("aaaa-bbbb-cccc-dddd"), "AAAA-BBBB-CCCC-DDDD");
  const fp = "AAAA-BBBB-CCCC-DDDD";
  const { token, claims } = gw.mintDeviceToken(fp);
  assert.ok(token.startsWith("cx."));
  const v = gw.verifyDeviceToken(token);
  assert.ok(v && v.mid === fp && v.exp === claims.exp);
  assert.equal(gw.verifyDeviceToken("cx.bad.sig"), null);
  assert.equal(gw.verifyDeviceToken(""), null);
  // 篡改载荷必拒
  const [h, body] = token.split(".");
  assert.equal(gw.verifyDeviceToken(`${h}.${body}x.${token.split(".")[2]}`), null);

  // ── 托管 instance_id：同指纹两实例额度键隔离（各耗 1 字，不扰动后续双层水位）──
  const ca = gw.mintDeviceToken(fp, undefined, "iso_a").claims;
  const cb = gw.mintDeviceToken(fp, undefined, "iso_b").claims;
  assert.equal(gw.quotaSubject(ca), "IID:iso_a");
  assert.ok((await gw.consumeQuota(ca, 1)).ok);
  assert.ok((await gw.consumeQuota(cb, 1)).ok);
  assert.equal((await gw.quotaSnapshot(ca)).used, 1);
  assert.equal((await gw.quotaSnapshot(cb)).used, 1);

  // ── model / max_tokens 钳制 ──
  // 退役名归一：老客户端令牌里的 deepseek-chat / deepseek-v4-flash → 现役 deepseek-flash
  assert.equal(gw.publicModel(), "deepseek-flash");
  assert.equal(gw.clampModel("deepseek-v4-flash"), "deepseek-flash");
  assert.equal(gw.clampModel("deepseek-chat"), "deepseek-flash");
  assert.equal(gw.clampModel("deepseek-v4-pro"), "deepseek-v4-pro"); // 白名单内非退役名原样
  assert.equal(gw.clampModel("deepseek-reasoner"), "deepseek-flash"); // 白名单外回落默认
  assert.equal(gw.clampModel(undefined), "deepseek-flash");
  assert.equal(gw.normalizeVendorModel("deepseek-chat", "https://api.siliconflow.cn/v1"), "deepseek-chat");

  // ── 按上游主机关思维链（与引擎 vendor_params 同口径）──
  assert.deepEqual(gw.thinkingOffFields("https://api.deepseek.com/chat/completions", "deepseek-flash"), {
    thinking: { type: "disabled" },
  });
  assert.deepEqual(
    gw.thinkingOffFields("https://api.siliconflow.cn/v1/chat/completions", "deepseek-ai/DeepSeek-V4-Flash"),
    { enable_thinking: false }
  );
  assert.deepEqual(gw.thinkingOffFields("https://api.siliconflow.cn/v1/chat/completions", "Qwen/Qwen2.5-7B-Instruct"), {});
  assert.deepEqual(gw.thinkingOffFields("https://api.openai.com/v1/chat/completions", "gpt-4o-mini"), {});
  const primaryChoice = gw.chatUpstreamChoice({ model: "deepseek-chat", messages: [] });
  assert.equal(primaryChoice.key, "primary");
  assert.equal(primaryChoice.model, "deepseek-flash");
  const p1 = gw.buildChatPayload(
    { model: "deepseek-chat", messages: [{ role: "user", content: "hi" }], max_tokens: 99999, thinking: { type: "enabled" }, stream: true },
    primaryChoice
  );
  assert.equal(p1.model, "deepseek-flash");
  assert.equal(p1.max_tokens, 2048);
  assert.equal(p1.stream, false);
  assert.deepEqual(p1.thinking, { type: "disabled" }); // 客户端要开也被网关口径覆盖
  assert.equal("enable_thinking" in p1, false);
  const backupChoice: import("./ai-gateway").ChatUpstreamChoice = {
    key: "backup",
    model: "deepseek-ai/DeepSeek-V4-Flash",
    endpoint: "https://api.siliconflow.cn/v1/chat/completions",
    apiKey: "sk-backup-unit",
  };
  const p2 = gw.buildChatPayload({ model: "deepseek-chat", messages: [] }, backupChoice);
  assert.equal(p2.model, "deepseek-ai/DeepSeek-V4-Flash");
  assert.equal(p2.enable_thinking, false);
  assert.equal("thinking" in p2, false);
  // 慢模型冷却 → 备用（硅基 V4-Flash）
  assert.equal(gw.noteChatLatency("deepseek-flash", 46_000), true);
  const cooled = gw.chatUpstreamChoice({ model: "deepseek-flash", messages: [] });
  assert.equal(cooled.key, "backup");
  assert.equal(cooled.model, "deepseek-ai/DeepSeek-V4-Flash");
  assert.ok(cooled.endpoint.includes("siliconflow"));
  assert.equal(gw.clampMaxTokens(99999), 2048);
  assert.equal(gw.clampMaxTokens(100), 100);
  assert.equal(gw.clampMaxTokens(undefined), 1024);

  // ── 字符估算 ──
  assert.equal(
    gw.estimateRequestChars({ messages: [{ role: "user", content: "你好世界" }] }),
    4
  );

  // ── 双层额度：单机 100 / 全局 150（前面 iso_* 已用全局 2）──
  const a = await gw.consumeQuota("AAAA-0000-0000-0001", 60);
  assert.ok(a.ok && a.remaining === 40);
  const a2 = await gw.consumeQuota("AAAA-0000-0000-0001", 50); // 60+50 > 100 → 单机拒
  assert.ok(!a2.ok && a2.which === "machine");
  const b = await gw.consumeQuota("AAAA-0000-0000-0002", 80); // 全局 2+60+80=142 ≤ 150
  assert.ok(b.ok);
  const c = await gw.consumeQuota("AAAA-0000-0000-0003", 20); // 142+20 > 150 → 全局拒
  assert.ok(!c.ok && c.which === "global");
  const snap = await gw.quotaSnapshot("AAAA-0000-0000-0003");
  assert.equal(snap.used, 0);
  assert.equal(snap.global_used, 142);
  assert.equal(snap.busy, false);
  const d = await gw.consumeQuota("AAAA-0000-0000-0003", 8); // 142+8=150 → busy
  assert.ok(d.ok);
  assert.equal((await gw.quotaSnapshot("AAAA-0000-0000-0003")).busy, true);

  // ── 当日聚合（console 卡）──
  const stats = await gw.gatewayDayStats();
  assert.equal(stats.machines, 5); // iso_a/iso_b + 0001/0002/0003（__GLOBAL__ 不算）
  assert.equal(stats.chars, 150);
  assert.equal(stats.machine_budget, 100);

  // ── 托管 instance_id：令牌带 iid；额度主体键按实例（同指纹不同 IID 不串账）──
  // 注意：本文件前面的双层额度用例已耗尽全局预算，此处只钉 claims/键（消费隔离用独立键证明）。
  assert.equal(gw.normalizeInstanceId("Zhiliao_Pilot!"), "zhiliao_pilot");
  assert.equal(gw.normalizeInstanceId("***"), "");
  const { token: tokH, claims: cH } = gw.mintDeviceToken(fp, undefined, "zhiliao_pilot");
  assert.equal(cH.iid, "zhiliao_pilot");
  assert.equal(gw.quotaSubject(cH), "IID:zhiliao_pilot");
  assert.equal(gw.quotaSubject({ mid: fp }), fp); // 无 iid → 回落指纹
  const vH = gw.verifyDeviceToken(tokH);
  assert.ok(vH && vH.iid === "zhiliao_pilot" && vH.mid === fp);
  assert.notEqual(
    gw.quotaSubject(gw.mintDeviceToken(fp, undefined, "tenant_a").claims),
    gw.quotaSubject(gw.mintDeviceToken(fp, undefined, "tenant_b").claims)
  );

  // ── 识图：模型识别 + 中继状态 ──
  assert.equal(gw.isVisionModel("qwen3-vl:8b-instruct"), true);
  assert.equal(gw.isVisionModel("qwen2.5vl:7b"), true);
  assert.equal(gw.isVisionModel("deepseek-chat"), false);
  assert.equal(gw.isVisionModel(""), false);
  const vs = gw.visionRelayStatus();
  assert.equal(typeof vs.enabled, "boolean");
  assert.ok(vs.canonical_model.length > 0);

  // ── 识图 num_ctx（#213，2026-09-06）：钧机实锤 prompt+图 ≈ 4170 tokens > Ollama 缺省 4096 ──
  // 期望窗口必须装得下实测请求并留余量；随请求声明 options.num_ctx（生效点是中继 Modelfile，
  // 见 VISION_NUM_CTX 注释——这里钉的是线上契约不被回退）。
  assert.ok(gw.VISION_NUM_CTX >= 8192, `VISION_NUM_CTX=${gw.VISION_NUM_CTX} 装不下实测 4181 tokens 的识图请求余量`);
  const vp = gw.buildVisionPayload({
    model: "qwen2.5vl:7b", max_tokens: 700, messages: [], options: { temperature: 0 },
  }) as { model: string; stream: boolean; options: { num_ctx: number; temperature: number } };
  assert.equal(vp.model, vs.canonical_model);          // 客户端自报模型一律改写为规范 VLM
  assert.equal(vp.stream, false);
  assert.equal(vp.options.num_ctx, gw.VISION_NUM_CTX);
  assert.equal(vp.options.temperature, 0);            // 客户端 options 保留，只补 num_ctx
  // 中继 400 形状识别（诊断包 MG65CT 原文）：解析 n_prompt_tokens / n_ctx；其它 400 与非 400 不误报
  const relay400 = JSON.stringify({ error: { code: 400,
    message: "request (4170 tokens) exceeds the available context size (4096 tokens), try increasing it",
    type: "exceed_context_size_error", n_prompt_tokens: 4170, n_ctx: 4096 } });
  const ovf = gw.visionContextOverflow(400, relay400);
  assert.ok(ovf);
  assert.equal(ovf!.n_prompt_tokens, 4170);
  assert.equal(ovf!.n_ctx, 4096);
  assert.equal(ovf!.expected_ctx, gw.VISION_NUM_CTX);
  const ovf2 = gw.visionContextOverflow(400, "request (4181 tokens) exceeds the available context size (4096 tokens)");
  assert.ok(ovf2 && ovf2.n_prompt_tokens === 4181 && ovf2.n_ctx === 4096);
  assert.equal(gw.visionContextOverflow(400, JSON.stringify({ error: { message: "bad image" } })), null);
  assert.equal(gw.visionContextOverflow(200, relay400), null);
  // 告警节流：首条放行，间隔内第二条压掉，过了间隔再放
  const t0 = Date.now();
  assert.equal(gw.shouldAlertVisionOverflow(t0), true);
  assert.equal(gw.shouldAlertVisionOverflow(t0 + 60_000), false);
  assert.equal(gw.shouldAlertVisionOverflow(t0 + gw.VISION_OVERFLOW_ALERT_GAP_MS + 1), true);

  // ── 嵌入中继（P0-1，B126 根因）：沿用识图隧道 + 字符估算 + 计额下限 ──
  // 「enabled」是这条路由的生死开关：为 false 时 route 直接回 503，而
  // 503 与 404-HTML 的区别正是本次修复的全部意义（客户端能解析成 JSON 错误）。
  assert.equal(gw.embedRelayEnabled(), true, "未显式配 EMBED_RELAY_URLS 时须继承识图中继");
  const es = gw.embedRelayStatus();
  assert.deepEqual(
    es.relays.map((r) => r.url),
    ["http://127.0.0.1:18411/v1", "http://127.0.0.1:18412/v1"]
  );
  assert.equal(es.canonical_model, "bge-m3");
  assert.equal(gw.estimateEmbedChars({ input: "你好世界" }), 4);
  assert.equal(gw.estimateEmbedChars({ input: ["abc", "de"] }), 5);
  assert.equal(gw.estimateEmbedChars({ input: [] }), 0);
  assert.equal(gw.estimateEmbedChars({}), 0);          // 无 input → route 回 400
  assert.equal(gw.estimateEmbedChars({ input: 42 }), 0);
  assert.ok(gw.EMBED_CHAR_MIN_COST > 0);

  // ── 超时分层不变量（B124 实锤，2026-08-28）──
  // 内层必须最小：中继尝试 × 台数 ≤ 路由预算 ≤ nginx。倒挂时超时由 nginx 先响，
  // 客户端收到 HTML 504 而非我们的 JSON —— 那就成了「静默回落默认音」。
  const B = gw.ROUTE_BUDGET_MS;
  const relayCount = 2; // TTS/识图/嵌入都是双活；ASR 单点
  assert.ok(gw.TTS_ATTEMPT_MS * relayCount <= B.tts,
    `TTS 单台上限 ${gw.TTS_ATTEMPT_MS}ms × ${relayCount} 必须装进路由预算 ${B.tts}ms`);
  assert.ok(gw.ASR_ATTEMPT_MS <= B.asr);
  assert.ok(gw.EMBED_ATTEMPT_MS * relayCount <= B.embed);
  for (const [name, ms] of Object.entries(B)) {
    if (name === "nginx_read") continue;
    assert.ok(ms <= B.nginx_read,
      `路由预算 ${name}=${ms}ms 超过 nginx proxy_read_timeout ${B.nginx_read}ms → 客户端会收到 nginx 的 HTML 504`);
  }

  // ── 生产环境缺 AI_GATEWAY_SECRET → 网关整体禁用 ──
  assert.equal(gw.gatewayEnabled(), true);
  const oldEnv = process.env.NODE_ENV;
  const oldSecret = process.env.AI_GATEWAY_SECRET;
  (process.env as Record<string, string | undefined>).NODE_ENV = "production";
  delete process.env.AI_GATEWAY_SECRET;
  assert.equal(gw.gatewayEnabled(), false);
  process.env.AI_GATEWAY_SECRET = oldSecret;
  assert.equal(gw.gatewayEnabled(), true);
  (process.env as Record<string, string | undefined>).NODE_ENV = oldEnv;

  console.log("ai-gateway.test.ts OK");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
