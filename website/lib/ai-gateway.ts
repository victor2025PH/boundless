/**
 * ChatX 托管 AI 网关：设备令牌签发 + 把 OpenAI 形态请求代理到厂商 DeepSeek Key。
 *
 * 安全模型：
 * - 客户端永不持有 DEEPSEEK_API_KEY；只持有短时 HMAC 设备令牌（绑机器指纹）。
 * - 令牌只发给**试用台账里存在的指纹**（trial-claim-store，领取时留联系方式）——
 *   这是唯一防「脚本刷指纹白嫖」的闸门，与 7 天试用同一条生命线。
 * - 生产环境必须显式配 AI_GATEWAY_SECRET（不再从 API Key 派生）；缺失=网关整体禁用。
 * - 双层额度：单机日额度 + 全局日预算（成本熔断），钳制 model 白名单与 max_tokens。
 *
 * 部署清单（VPS）：DEEPSEEK_API_KEY、AI_GATEWAY_SECRET、NEXT_PUBLIC_SITE_URL 三个必配。
 */
import crypto from "crypto";
import Database from "better-sqlite3";
import { appendFile, mkdir, rename, stat } from "fs/promises";
import { mkdirSync } from "fs";
import path from "path";
import { DATA_DIR } from "./data-dir";

const VENDOR_ENDPOINT =
  process.env.DEEPSEEK_BASE_URL || "https://api.deepseek.com/chat/completions";
const VENDOR_MODEL = process.env.DEEPSEEK_MODEL || "deepseek-chat";

/** 单机每日字符预算（入站 + 出站），防单机刷爆。 */
export const DAILY_CHAR_BUDGET = Number(process.env.AI_GATEWAY_DAILY_CHARS || 50000);
/** 全局每日字符预算（所有机器合计）——云账单的总熔断闸。 */
export const GLOBAL_DAILY_CHAR_BUDGET = Number(
  process.env.AI_GATEWAY_GLOBAL_DAILY_CHARS || 2_000_000
);
/** 设备令牌有效期（秒）。默认 30 天：桌面端只在启动时换新，太短会在长开机器上过期。 */
export const TOKEN_TTL_SEC = Number(process.env.AI_GATEWAY_TOKEN_TTL || 30 * 24 * 3600);
/** 服务端 max_tokens 钳制（客户端自报不可信）。 */
export const MAX_TOKENS_CAP = Number(process.env.AI_GATEWAY_MAX_TOKENS || 2048);

/** model 白名单：默认只放厂商默认模型；多模型用逗号分隔 env 配置。 */
const MODEL_ALLOWLIST: string[] = (process.env.AI_GATEWAY_MODELS || VENDOR_MODEL)
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean);

// ── 识图中继（我们自己的 GPU VLM，经 117→VPS 反向隧道暴露到 VPS localhost）──
// VISION_RELAY_URLS 逗号列表 = 多机双活（176/140），按序尝试、失败短冷却降权；
// 回落单数 VISION_RELAY_URL。未设 = 识图禁用（暗态），客户端自行回落。
const VISION_RELAY_URLS: string[] = (
  process.env.VISION_RELAY_URLS || process.env.VISION_RELAY_URL || ""
)
  .split(",")
  .map((s) => s.trim().replace(/\/+$/, ""))
  .filter(Boolean);
const VISION_RELAY_URL = VISION_RELAY_URLS[0] || "";
/** 规范识图模型：两台中继都装的那个。网关统一改写 model —— 已发布客户端
 *  （0.2.7 注入 qwen2.5vl:7b，仅 176 有）也能被任一中继服务，双活才真互换。 */
const VISION_MODEL_CANONICAL = (
  process.env.VISION_MODEL_CANONICAL || "qwen3-vl:8b-instruct"
).trim();
/** 中继失败冷却（毫秒）：某机忙/宕 → 短暂降权，不每次都撞它。 */
const RELAY_COOLDOWN_MS = Number(process.env.VISION_RELAY_COOLDOWN_MS || 60000);
const _relayCooldown = new Map<string, number>();

/**
 * 识图请求期望的上下文窗口（#213，2026-09-06）。
 *
 * 钧机 1.0.74 实锤：识图 prompt + 一张图 ≈ 4170–4181 tokens，而 Ollama 缺省 num_ctx=4096
 * → 中继回 400 `exceed_context_size_error`，所有走网关的识图必败。
 *
 * **生效点在中继机的 Modelfile**（176/140 的 `qwen3-vl:8b-instruct` 已 `PARAMETER num_ctx 8192`，
 * 原 4k 版留作 `qwen3-vl:8b-instruct-orig4k` 回滚点）——Ollama 0.31/0.32 的 OpenAI 兼容层
 * **忽略** 请求体里的 `options`（2026-09-06 实测：带 options.num_ctx=8192 发 /v1，`ollama ps`
 * 的 CONTEXT 仍 4096；引擎侧 vision_client.py 2026-08-15 也记过同一结论）。这里仍随请求
 * 声明 `options.num_ctx`：一是把期望值写进线上契约、Ollama 若日后放开即自动生效，二是
 * `visionContextOverflow()` 用它和中继回报的 n_ctx 对照，Modelfile 参数被谁重拉丢掉时
 * 告警能直接说出「中继 n_ctx=4096 < 期望 8192」。
 */
export const VISION_NUM_CTX = Number(process.env.VISION_NUM_CTX || 8192);

export type VisionContextOverflow = {
  n_prompt_tokens: number | null;
  n_ctx: number | null;
  expected_ctx: number;
};

/**
 * 识别中继的「上下文超限」400（Ollama 形状：`exceed_context_size_error` /
 * "request (N tokens) exceeds the available context size (M tokens)"）。非该错误返回 null。
 */
export function visionContextOverflow(status: number, bodyText: string): VisionContextOverflow | null {
  if (status !== 400) return null;
  const t = String(bodyText || "");
  if (!/exceed_context_size_error|exceeds the available context size/i.test(t)) return null;
  const num = (re: RegExp): number | null => {
    const m = re.exec(t);
    return m ? Number(m[1]) : null;
  };
  return {
    n_prompt_tokens: num(/"n_prompt_tokens"\s*:\s*(\d+)/) ?? num(/request \((\d+) tokens\)/i),
    n_ctx: num(/"n_ctx"\s*:\s*(\d+)/) ?? num(/context size \((\d+) tokens\)/i),
    expected_ctx: VISION_NUM_CTX,
  };
}

/** 上下文超限告警节流：同一进程 30 分钟最多推一条（一张图失败会连着重试，别刷屏）。 */
export const VISION_OVERFLOW_ALERT_GAP_MS = Number(process.env.VISION_OVERFLOW_ALERT_GAP_MS || 30 * 60 * 1000);
let _lastOverflowAlertAt = 0;

export function shouldAlertVisionOverflow(now = Date.now()): boolean {
  if (now - _lastOverflowAlertAt < VISION_OVERFLOW_ALERT_GAP_MS) return false;
  _lastOverflowAlertAt = now;
  return true;
}

/** 识图中继请求体：model 改写为规范 VLM + 声明期望 num_ctx（见 VISION_NUM_CTX 注释）。 */
export function buildVisionPayload(body: Record<string, unknown>): Record<string, unknown> {
  const prior = body.options && typeof body.options === "object" ? (body.options as Record<string, unknown>) : {};
  return {
    ...body,
    model: VISION_MODEL_CANONICAL,
    stream: false,
    max_tokens: clampMaxTokens(body.max_tokens),
    options: { ...prior, num_ctx: VISION_NUM_CTX },
  };
}

function orderedRelays(): string[] {
  const now = Date.now();
  const fresh: string[] = [];
  const cooled: string[] = [];
  for (const u of VISION_RELAY_URLS) {
    if ((_relayCooldown.get(u) || 0) > now) cooled.push(u);
    else fresh.push(u);
  }
  return [...fresh, ...cooled]; // 全在冷却也要硬试（比直接失败好）
}
const VISION_MODELS: string[] = (process.env.VISION_MODELS || "qwen2.5vl:7b,qwen2.5vl:32b,qwen2.5vl:72b")
  .split(",")
  .map((s) => s.trim().toLowerCase())
  .filter(Boolean);
// 识图一次调用的额度折算（图片 token 远多于纯文本，按固定成本计，防按字符低估）
export const VISION_CHAR_COST = Number(process.env.AI_GATEWAY_VISION_CHARS || 1500);

export function visionRelayEnabled(): boolean {
  return Boolean(VISION_RELAY_URL);
}

export function isVisionModel(model: unknown): boolean {
  const m = String(model || "").trim().toLowerCase();
  if (!m) return false;
  if (VISION_MODELS.includes(m)) return true;
  // 兜底特征：常见 VLM 名含 vl / llava / -v（glm-4v）
  return /(\bvl\b|vl:|llava|-v\b|4v)/.test(m);
}

export async function proxyVision(
  body: Record<string, unknown>,
  signal?: AbortSignal
): Promise<Response> {
  const relays = orderedRelays();
  if (!relays.length) throw new Error("no_vision_relay");
  // 中继是 Ollama /v1（keyless）；model 统一改写成规范 VLM（两机都装 → 可互换）
  const payload = buildVisionPayload(body);
  let lastErr: unknown = null;
  for (const base of relays) {
    try {
      const r = await fetch(`${base}/chat/completions`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer ollama" },
        body: JSON.stringify(payload),
        signal,
      });
      // 5xx = 该机有问题（忙/模型缺）→ 冷却降权，试下一台
      if (r.status >= 500 && relays.length > 1) {
        _relayCooldown.set(base, Date.now() + RELAY_COOLDOWN_MS);
        lastErr = new Error(`relay_${r.status}`);
        continue;
      }
      return r;
    } catch (e) {
      // 网络级失败（隧道断）→ 冷却降权，试下一台
      _relayCooldown.set(base, Date.now() + RELAY_COOLDOWN_MS);
      lastErr = e;
    }
  }
  throw lastErr || new Error("all_vision_relays_failed");
}

// ── 语音中继（2026-08-03）：克隆 TTS 与 GPU ASR(8765) 经同一条 117→VPS
//    反向隧道暴露到 VPS localhost，网关按设备令牌鉴权转发——把「非局域网机器用
//    集群算力生成语音 / 听懂语音」补齐到与识图同一安全模型。
//    2026-08-29 起主中继上游=104:7865 IndexTTS-2（同 /v1/tts/clone 契约；备=140:7852
//    CosyVoice）。健康形状两家不同：7852 出 {ok,models_loaded}、7865 出
//    {status:"ok",model_loaded}，ttsRelayHealth 两种都认。
//    TTS_RELAY_URLS  逗号列表（104 主 / 140 备）：http://127.0.0.1:18413,http://127.0.0.1:18414
//    ASR_RELAY_URLS  须含 /v1 后缀（OpenAI 形态）：http://127.0.0.1:18415/v1
//    AH_SERVICE_TOKEN 集群内部 X-AH-Svc 令牌——只在 VPS env，**永不下发客户端**
//    （客户端只持 cx.* 设备令牌；集群令牌由网关侧代注）。
const TTS_RELAY_URLS: string[] = (process.env.TTS_RELAY_URLS || "")
  .split(",")
  .map((s) => s.trim().replace(/\/+$/, ""))
  .filter(Boolean);
const ASR_RELAY_URLS: string[] = (process.env.ASR_RELAY_URLS || "")
  .split(",")
  .map((s) => s.trim().replace(/\/+$/, ""))
  .filter(Boolean);
const AH_SERVICE_TOKEN = (process.env.AH_SERVICE_TOKEN || "").trim();
/** TTS 一次合成的最低计额（字符）：短句也占 GPU 一轮串行合成，纯按字符会低估。 */
export const TTS_CHAR_MIN_COST = Number(process.env.AI_GATEWAY_TTS_MIN_CHARS || 60);
/** ASR 一次转写的固定计额（字符）：语音时长网关不可靠可知，按次折算。 */
export const ASR_CHAR_COST = Number(process.env.AI_GATEWAY_ASR_CHARS || 200);

const _ttsCooldown = new Map<string, number>();
const _asrCooldown = new Map<string, number>();

function orderedOf(urls: string[], cooldown: Map<string, number>): string[] {
  const now = Date.now();
  const fresh: string[] = [];
  const cooled: string[] = [];
  for (const u of urls) {
    if ((cooldown.get(u) || 0) > now) cooled.push(u);
    else fresh.push(u);
  }
  return [...fresh, ...cooled]; // 全在冷却也要硬试（比直接失败好）
}

/** 单次中继尝试的上限（毫秒）：TTS 合成慢，但一台**病态**节点也不能吃掉整个预算。 */
export const TTS_ATTEMPT_MS = Number(process.env.AI_GATEWAY_TTS_ATTEMPT_MS || 40000);
export const ASR_ATTEMPT_MS = Number(process.env.AI_GATEWAY_ASR_ATTEMPT_MS || 30000);
export const EMBED_ATTEMPT_MS = Number(process.env.AI_GATEWAY_EMBED_ATTEMPT_MS || 12000);

/**
 * 各 AI 路由的**总**预算（毫秒）——超时分层的单一事实源。
 *
 * 不变量（内层必须最小，否则外层先响、客户端拿到的是 HTML 而不是我们的 JSON）：
 *     单次中继上限 × 中继台数  ≤  路由总预算  ≤  nginx proxy_read_timeout
 *
 * 2026-08-28 B124 实锤的就是这条被倒挂：nginx 没配 proxy_read_timeout（默认 60s）
 * 而 TTS 路由预算 90s、识图 120s → 每次超时都由 nginx 先响，客户端收到
 * `<html>504 Gateway Time-out</html>`。OpenAI/AvatarVoice 客户端都按 JSON 解析，
 * 于是「网关报错」变成「解析失败」，最终静默回落默认音（与 P0-1 的 embeddings
 * 404-HTML 是同一个病）。nginx 侧现配 180s，让应用预算永远是那个先响的。
 */
export const ROUTE_BUDGET_MS = {
  tts: 90_000,
  asr: 60_000,
  embed: 30_000,
  chat: 55_000,
  vision: 120_000,
  /** nginx `location ^~ /api/ai/` 的 proxy_read_timeout，必须 ≥ 上面所有值。 */
  nginx_read: 180_000,
} as const;

/**
 * 把「调用方总预算」与「单次尝试上限」合成一个 signal。
 *
 * 为什么必须有（2026-08-28 B124 实锤）：各 proxy* 原先把**同一个** AbortController
 * 传给循环里的每一次 fetch，于是第一台中继病态（TCP 通、就是不回包）时它会把整个
 * 总预算耗光，abort 一响整个循环就结束——**健康的备机永远轮不到**。当时 117:7852
 * 停摆、140:7852 单点病态（实测 12 字 70s），每发克隆都耗满 90s，再被 nginx 60s
 * 截成 HTML 504：客户端连 JSON 错误都拿不到，只能静默回落默认音。
 *
 * 语义：任一方超时都 abort 本次尝试；调用方总预算到了则**不再试下一台**
 * （由调用方 signal.aborted 判定），只是单台慢不再连坐其余节点。
 */
function attemptSignal(
  outer: AbortSignal | undefined,
  ms: number
): { signal: AbortSignal; release: () => void } {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), Math.max(1000, ms));
  const relay = () => ac.abort();
  if (outer) {
    if (outer.aborted) ac.abort();
    else outer.addEventListener("abort", relay, { once: true });
  }
  return {
    signal: ac.signal,
    release: () => {
      clearTimeout(timer);
      outer?.removeEventListener("abort", relay);
    },
  };
}

export function ttsRelayEnabled(): boolean {
  return TTS_RELAY_URLS.length > 0;
}

export function asrRelayEnabled(): boolean {
  return ASR_RELAY_URLS.length > 0;
}

/** 中继请求头：代注集群内部令牌（7852@140 等跨机节点要 X-AH-Svc 才放行）。 */
function relayHeaders(extra: Record<string, string>): Record<string, string> {
  const h: Record<string, string> = { ...extra };
  if (AH_SERVICE_TOKEN) h["X-AH-Svc"] = AH_SERVICE_TOKEN;
  return h;
}

/** 从请求头取设备令牌：Authorization Bearer 或 X-AH-Svc（avatar_voice 客户端走后者）。 */
export function extractDeviceToken(headers: Headers): string {
  const auth = headers.get("authorization") || "";
  const m = /^Bearer\s+(.+)$/i.exec(auth.trim());
  if (m && m[1].trim().startsWith("cx.")) return m[1].trim();
  const svc = (headers.get("x-ah-svc") || "").trim();
  return svc.startsWith("cx.") ? svc : "";
}

/** TTS 转发：JSON 原样透传到 7852 中继（117 主 / 140 备），5xx/网络失败冷却降权。 */
export async function proxyTts(
  path: string,
  rawJson: string,
  signal?: AbortSignal
): Promise<Response> {
  const relays = orderedOf(TTS_RELAY_URLS, _ttsCooldown);
  if (!relays.length) throw new Error("no_tts_relay");
  let lastErr: unknown = null;
  for (const base of relays) {
    const att = attemptSignal(signal, TTS_ATTEMPT_MS);
    try {
      const r = await fetch(`${base}${path}`, {
        method: "POST",
        headers: relayHeaders({ "Content-Type": "application/json" }),
        body: rawJson,
        signal: att.signal,
      });
      if (r.status >= 500 && relays.length > 1) {
        _ttsCooldown.set(base, Date.now() + RELAY_COOLDOWN_MS);
        lastErr = new Error(`relay_${r.status}`);
        continue;
      }
      return r;
    } catch (e) {
      _ttsCooldown.set(base, Date.now() + RELAY_COOLDOWN_MS);
      lastErr = e;
      if (signal?.aborted) break;   // 总预算已到：别再拖着下一台白试
    } finally {
      att.release();
    }
  }
  throw lastErr || new Error("all_tts_relays_failed");
}

/** ASR 转发：multipart 原样透传（含 boundary 的 Content-Type 一并带过去）。 */
export async function proxyAsr(
  body: ArrayBuffer,
  contentType: string,
  signal?: AbortSignal
): Promise<Response> {
  const relays = orderedOf(ASR_RELAY_URLS, _asrCooldown);
  if (!relays.length) throw new Error("no_asr_relay");
  let lastErr: unknown = null;
  for (const base of relays) {
    const att = attemptSignal(signal, ASR_ATTEMPT_MS);
    try {
      const r = await fetch(`${base}/audio/transcriptions`, {
        method: "POST",
        headers: relayHeaders(
          contentType ? { "Content-Type": contentType } : {}
        ),
        body,
        signal: att.signal,
      });
      if (r.status >= 500 && relays.length > 1) {
        _asrCooldown.set(base, Date.now() + RELAY_COOLDOWN_MS);
        lastErr = new Error(`relay_${r.status}`);
        continue;
      }
      return r;
    } catch (e) {
      _asrCooldown.set(base, Date.now() + RELAY_COOLDOWN_MS);
      lastErr = e;
      if (signal?.aborted) break;
    } finally {
      att.release();
    }
  }
  throw lastErr || new Error("all_asr_relays_failed");
}

// TTS /health 透传（15s 缓存）：客户端 AvatarVoiceClient 健康预检据此决定端点
// 次序——必须真探中继（隧道断/服务半死时如实报不健康，客户端才会回落），
// 但带缓存防「每台客户机每 30s 一探」打穿隧道。
let _ttsHealth: { ts: number; ok: boolean } = { ts: 0, ok: false };

export async function ttsRelayHealth(): Promise<{ ok: boolean; models_loaded: boolean }> {
  if (!TTS_RELAY_URLS.length) return { ok: false, models_loaded: false };
  const now = Date.now();
  if (now - _ttsHealth.ts < 15000) {
    return { ok: _ttsHealth.ok, models_loaded: _ttsHealth.ok };
  }
  let ok = false;
  for (const base of orderedOf(TTS_RELAY_URLS, _ttsCooldown)) {
    try {
      const ac = new AbortController();
      const t = setTimeout(() => ac.abort(), 4000);
      // cache:"no-store" 是必须的：Next.js 会把 route handler 里的裸 GET fetch 写进
      // 持久 Data Cache（revalidate 默认一年）。2026-08-29 实锤：8/3 缓存的
      // {ok:true} 被原样回放 26 天，两台中继上游全死健康仍报绿——客户端预检
      // 选中网关、真合成打死隧道超时回落通用音，监控端却全程无恙。
      const r = await fetch(`${base}/health`, {
        cache: "no-store",
        headers: relayHeaders({ accept: "application/json" }),
        signal: ac.signal,
      });
      clearTimeout(t);
      if (r.ok) {
        const j = (await r.json().catch(() => null)) as
          | { ok?: unknown; models_loaded?: unknown;
              status?: unknown; model_loaded?: unknown }
          | null;
        // 两种上游健康形状都认：mfys CosyVoice(7852)={ok,models_loaded}；
        // IndexTTS-2(7865)={status:"ok",model_loaded}。字段缺省按就绪算
        // （与引擎侧 AvatarVoiceClient._probe 同口径）。
        const flagOk = j !== null && (j.ok === true || j.status === "ok");
        const loaded =
          j !== null && j.models_loaded !== false && j.model_loaded !== false;
        if (flagOk && loaded) {
          ok = true;
          break;
        }
      }
    } catch {
      /* try next relay */
    }
  }
  _ttsHealth = { ts: now, ok };
  return { ok, models_loaded: ok };
}

// ── 嵌入中继（2026-08-28，P0-1）：语义记忆召回 / 翻译语义闸门的向量来源。
//    客户端 ai_client.embed() 未配独立嵌入端点时回落对话客户端的 base_url，
//    也就是本网关 —— 于是它打的是 POST /api/ai/v1/embeddings。该路由此前**不存在**，
//    Next.js 回 404 **HTML 页**，OpenAI SDK 解析失败 → 连续 3 次即熔断 120s，
//    全网客户静默降级成纯关键词召回（B126「客户说过生日、AI 还反问」的根因）。
//    嵌入模型（bge-m3）与识图 VLM 同住 Ollama，复用同一条 117→VPS 反向隧道端口
//    （18411/18412），故默认直接沿用 VISION_RELAY_URLS，**无需新增隧道**。
const EMBED_RELAY_URLS: string[] = (
  process.env.EMBED_RELAY_URLS || process.env.VISION_RELAY_URLS || process.env.VISION_RELAY_URL || ""
)
  .split(",")
  .map((s) => s.trim().replace(/\/+$/, ""))
  .filter(Boolean);
/** 规范嵌入模型：两台中继都装的那个。客户端自报的模型名一律改写成它——
 *  已发布客户端写死 bge-m3，而向量库的维度必须全网一致，绝不能按客户端心情走。 */
const EMBED_MODEL_CANONICAL = (process.env.EMBED_MODEL_CANONICAL || "bge-m3").trim();
/** 嵌入一次调用的最低计额（字符）：短句嵌入也占一轮 GPU，纯按字符会低估。 */
export const EMBED_CHAR_MIN_COST = Number(process.env.AI_GATEWAY_EMBED_MIN_CHARS || 20);

const _embedCooldown = new Map<string, number>();

export function embedRelayEnabled(): boolean {
  return EMBED_RELAY_URLS.length > 0;
}

/** 嵌入入参字符数（input 可为 string 或 string[]）——计额与观测口径。 */
export function estimateEmbedChars(body: unknown): number {
  try {
    const input = (body as { input?: unknown })?.input;
    if (typeof input === "string") return input.length;
    if (Array.isArray(input)) {
      let n = 0;
      for (const it of input) if (typeof it === "string") n += it.length;
      return n;
    }
    return 0;
  } catch {
    return 0;
  }
}

/**
 * 嵌入转发：model 统一改写为规范模型，按序试中继、5xx/网络失败冷却降权。
 * 契约与 Ollama 的 OpenAI 兼容层一致（POST {base}/embeddings，base 已含 /v1）。
 */
export async function proxyEmbeddings(
  body: Record<string, unknown>,
  signal?: AbortSignal
): Promise<Response> {
  const relays = orderedOf(EMBED_RELAY_URLS, _embedCooldown);
  if (!relays.length) throw new Error("no_embed_relay");
  const payload = { ...body, model: EMBED_MODEL_CANONICAL };
  let lastErr: unknown = null;
  for (const base of relays) {
    const att = attemptSignal(signal, EMBED_ATTEMPT_MS);
    try {
      const r = await fetch(`${base}/embeddings`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer ollama" },
        body: JSON.stringify(payload),
        signal: att.signal,
      });
      if (r.status >= 500 && relays.length > 1) {
        _embedCooldown.set(base, Date.now() + RELAY_COOLDOWN_MS);
        lastErr = new Error(`relay_${r.status}`);
        continue;
      }
      return r;
    } catch (e) {
      _embedCooldown.set(base, Date.now() + RELAY_COOLDOWN_MS);
      lastErr = e;
      if (signal?.aborted) break;
    } finally {
      att.release();
    }
  }
  throw lastErr || new Error("all_embed_relays_failed");
}

/** 观测：嵌入中继与冷却态（console 卡/排障用；不含任何密钥）。 */
export function embedRelayStatus(): {
  enabled: boolean;
  canonical_model: string;
  relays: Array<{ url: string; cooling: boolean }>;
} {
  const now = Date.now();
  return {
    enabled: EMBED_RELAY_URLS.length > 0,
    canonical_model: EMBED_MODEL_CANONICAL,
    relays: EMBED_RELAY_URLS.map((u) => ({
      url: u,
      cooling: (_embedCooldown.get(u) || 0) > now,
    })),
  };
}

/** 观测：识图中继与冷却态（console 卡/排障用；不含任何密钥）。 */
export function visionRelayStatus(): {
  enabled: boolean;
  canonical_model: string;
  relays: Array<{ url: string; cooling: boolean }>;
} {
  const now = Date.now();
  return {
    enabled: VISION_RELAY_URLS.length > 0,
    canonical_model: VISION_MODEL_CANONICAL,
    relays: VISION_RELAY_URLS.map((u) => ({
      url: u,
      cooling: (_relayCooldown.get(u) || 0) > now,
    })),
  };
}

const QUOTA_DB = process.env.AI_GATEWAY_QUOTA_DB || path.join(DATA_DIR, "ai-gateway.db");
const GW_LOG = process.env.AI_GATEWAY_LOG || path.join(DATA_DIR, "ai-gateway.jsonl");
const GW_LOG_MAX_BYTES = 10 * 1024 * 1024; // 超过即轮转一份 .1（保一代，观测数据非审计）
const GLOBAL_KEY = "__GLOBAL__";

export type DeviceClaims = {
  v: 1;
  mid: string;
  /** 托管实例 ID（可选）。有则额度键用实例维度，同机多租户互不挤额度。 */
  iid?: string;
  iat: number;
  exp: number;
};

// ── 额度库：better-sqlite3（WAL）——多进程/PM2 cluster 下事务原子，
//    替代首版 JSON 文件 + 进程内串行链（只在单进程下安全）。 ──
let _db: Database.Database | null = null;

function db(): Database.Database {
  if (_db) return _db;
  mkdirSync(path.dirname(QUOTA_DB), { recursive: true });
  _db = new Database(QUOTA_DB);
  _db.pragma("journal_mode = WAL");
  _db.exec(
    "CREATE TABLE IF NOT EXISTS gw_quota (" +
    " day TEXT NOT NULL, mid TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0," +
    " PRIMARY KEY (day, mid))"
  );
  // 按主体（机器指纹 / IID:实例）的日额度覆写：给付费/重点客户单独提额，
  // 不动 env 全局默认（改 env 要重启且影响所有机器）。经 /api/admin/gw-budget 管理。
  _db.exec(
    "CREATE TABLE IF NOT EXISTS gw_budget (" +
    " mid TEXT PRIMARY KEY, budget INTEGER NOT NULL," +
    " note TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL DEFAULT 0)"
  );
  // Telegram 凭据粘定表：机器指纹 → 分到的 api_id（同机永远同 api_id，
  // 因 pyrogram session 与 api_id 绑定，换组会触发 Telegram 风控——与 LAN 池同不变量）。
  _db.exec(
    "CREATE TABLE IF NOT EXISTS tg_cred_assign (" +
    " mid TEXT PRIMARY KEY, api_id TEXT NOT NULL, assigned_at INTEGER NOT NULL)"
  );
  return _db;
}

function usedOf(day: string, mid: string): number {
  const row = db().prepare("SELECT used FROM gw_quota WHERE day=? AND mid=?").get(day, mid) as
    | { used: number }
    | undefined;
  return row?.used || 0;
}

/** 主体覆写额度（无覆写返回 null）。 */
function budgetOverrideOf(mid: string): number | null {
  const row = db().prepare("SELECT budget FROM gw_budget WHERE mid=?").get(mid) as
    | { budget: number }
    | undefined;
  return row && Number.isFinite(row.budget) && row.budget > 0 ? row.budget : null;
}

/** 主体生效日额度：覆写优先，否则 env 默认。 */
export function budgetOf(subject: string | Pick<DeviceClaims, "mid" | "iid">): number {
  const mid = quotaSubject(subject);
  return budgetOverrideOf(mid) ?? DAILY_CHAR_BUDGET;
}

/** 设置/更新主体额度覆写（budget<=0 视为清除）。subject 原样规整为额度键。 */
export function setBudgetOverride(
  subject: string,
  budget: number,
  note = ""
): { subject: string; budget: number } {
  const mid = quotaSubject(String(subject || "").trim());
  if (!mid || mid === GLOBAL_KEY) throw new Error("bad_subject");
  const b = Math.floor(Number(budget));
  if (!Number.isFinite(b) || b <= 0) {
    db().prepare("DELETE FROM gw_budget WHERE mid=?").run(mid);
    return { subject: mid, budget: DAILY_CHAR_BUDGET };
  }
  db()
    .prepare(
      "INSERT INTO gw_budget (mid, budget, note, updated_at) VALUES (?, ?, ?, ?)" +
      " ON CONFLICT(mid) DO UPDATE SET budget=excluded.budget, note=excluded.note," +
      " updated_at=excluded.updated_at"
    )
    .run(mid, b, String(note || "").slice(0, 200), Math.floor(Date.now() / 1000));
  return { subject: mid, budget: b };
}

/** 列出全部覆写（admin 观测）。 */
export function listBudgetOverrides(): Array<{
  subject: string;
  budget: number;
  note: string;
  updated_at: number;
  used_today: number;
}> {
  const day = dayKey();
  const rows = db()
    .prepare("SELECT mid, budget, note, updated_at FROM gw_budget ORDER BY updated_at DESC")
    .all() as Array<{ mid: string; budget: number; note: string; updated_at: number }>;
  return rows.map((r) => ({
    subject: r.mid,
    budget: r.budget,
    note: r.note || "",
    updated_at: r.updated_at || 0,
    used_today: usedOf(day, r.mid),
  }));
}

function pruneOldDays(): void {
  const cutoff = dayKey(new Date(Date.now() - 3 * 86400000));
  db().prepare("DELETE FROM gw_quota WHERE day < ?").run(cutoff);
}

function secretConfigured(): boolean {
  return Boolean((process.env.AI_GATEWAY_SECRET || "").trim());
}

/** 网关是否可用：有厂商 Key，且生产环境已显式配 HMAC 密钥。 */
export function gatewayEnabled(): boolean {
  if (!process.env.DEEPSEEK_API_KEY) return false;
  if (process.env.NODE_ENV === "production" && !secretConfigured()) return false;
  return true;
}

function hmacSecret(): string {
  const explicit = (process.env.AI_GATEWAY_SECRET || "").trim();
  if (explicit) return explicit;
  // 仅开发环境兜底派生；生产缺 secret 时 gatewayEnabled 已整体禁用，不会走到这里。
  const key = process.env.DEEPSEEK_API_KEY || "";
  return crypto.createHash("sha256").update(`chatx-gw-dev|${key}`).digest("hex");
}

export function normalizeFingerprint(raw: string): string {
  return String(raw || "")
    .trim()
    .toUpperCase()
    .replace(/[^A-Z0-9-]/g, "")
    .slice(0, 64);
}

/** 托管 instance_id：小写 [a-z0-9_-]，最长 64；非法 → 空（不写入 claims）。 */
export function normalizeInstanceId(raw: string): string {
  return String(raw || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]/g, "")
    .slice(0, 64);
}

/**
 * 额度主体键：有合法 iid → ``IID:<iid>``（按租户实例计量）；
 * 否则回落机器指纹 mid（桌面试用旧口径，向后兼容）。
 */
export function quotaSubject(claims: Pick<DeviceClaims, "mid" | "iid"> | string): string {
  if (typeof claims === "string") {
    // 兼容旧调用：consumeQuota(fingerprint, n) 仍可直接传 mid / IID:…
    return String(claims || "").trim();
  }
  const iid = normalizeInstanceId(claims.iid || "");
  if (iid) return `IID:${iid}`;
  return claims.mid;
}

/** model 钳制：白名单外一律回落默认（不拒——改造过的客户端也能用，只是用不了贵模型）。 */
export function clampModel(m: unknown): string {
  const s = String(m || "").trim();
  return MODEL_ALLOWLIST.includes(s) ? s : MODEL_ALLOWLIST[0];
}

export function clampMaxTokens(n: unknown): number {
  const v = Math.floor(Number(n));
  if (!Number.isFinite(v) || v <= 0) return Math.min(1024, MAX_TOKENS_CAP);
  return Math.min(v, MAX_TOKENS_CAP);
}

function b64url(buf: Buffer | string): string {
  const b = Buffer.isBuffer(buf) ? buf : Buffer.from(buf, "utf8");
  return b
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

function fromB64url(s: string): Buffer {
  const pad = s.length % 4 === 0 ? "" : "=".repeat(4 - (s.length % 4));
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/") + pad;
  return Buffer.from(b64, "base64");
}

export function mintDeviceToken(
  fingerprint: string,
  nowSec = Math.floor(Date.now() / 1000),
  instanceId?: string
): {
  token: string;
  claims: DeviceClaims;
} {
  const mid = normalizeFingerprint(fingerprint);
  const iid = normalizeInstanceId(instanceId || "");
  const claims: DeviceClaims = {
    v: 1,
    mid,
    iat: nowSec,
    exp: nowSec + TOKEN_TTL_SEC,
  };
  if (iid) claims.iid = iid;
  const body = b64url(JSON.stringify(claims));
  const sig = b64url(crypto.createHmac("sha256", hmacSecret()).update(body).digest());
  return { token: `cx.${body}.${sig}`, claims };
}

export function verifyDeviceToken(token: string, nowSec = Math.floor(Date.now() / 1000)): DeviceClaims | null {
  const t = String(token || "").trim();
  if (!t.startsWith("cx.")) return null;
  const parts = t.split(".");
  if (parts.length !== 3) return null;
  const [, body, sig] = parts;
  const expect = b64url(crypto.createHmac("sha256", hmacSecret()).update(body).digest());
  const a = Buffer.from(sig);
  const b = Buffer.from(expect);
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return null;
  try {
    const claims = JSON.parse(fromB64url(body).toString("utf8")) as DeviceClaims;
    if (claims?.v !== 1 || !claims.mid || !claims.exp) return null;
    if (claims.exp < nowSec) return null;
    if (!normalizeFingerprint(claims.mid)) return null;
    return claims;
  } catch {
    return null;
  }
}

function dayKey(d = new Date()): string {
  return d.toISOString().slice(0, 10);
}

export type QuotaSnapshot = {
  used: number;
  budget: number;
  remaining: number;
  global_used: number;
  global_budget: number;
  global_remaining: number;
  /** 全局预算耗尽（=试用通道繁忙，与单机额度用尽区分展示）。 */
  busy: boolean;
};

export async function quotaSnapshot(
  subject: string | Pick<DeviceClaims, "mid" | "iid">
): Promise<QuotaSnapshot> {
  // subject 可以是指纹、IID:…、或 claims；库键不再强制指纹格式（托管实例键是 IID:）
  const mid = quotaSubject(subject);
  const day = dayKey();
  const used = usedOf(day, mid);
  const gUsed = usedOf(day, GLOBAL_KEY);
  const budget = budgetOverrideOf(mid) ?? DAILY_CHAR_BUDGET;
  // 有显式覆写的主体不受全局熔断（管理员授予的额度不该被试用池保险丝掐掉）
  const overridden = budgetOverrideOf(mid) !== null;
  return {
    used,
    budget,
    remaining: Math.max(0, budget - used),
    global_used: gUsed,
    global_budget: GLOBAL_DAILY_CHAR_BUDGET,
    global_remaining: Math.max(0, GLOBAL_DAILY_CHAR_BUDGET - gUsed),
    busy: !overridden && gUsed >= GLOBAL_DAILY_CHAR_BUDGET,
  };
}

/**
 * 计费（主体 + 全局同一 sqlite 事务）。chars=0 只做快照式检查不落账。
 * subject = 机器指纹 mid，或托管 ``IID:<instance_id>``（经 quotaSubject(claims)）。
 * 返回 which：超的是哪一层（machine|global），前端据此分「明天恢复」vs「通道繁忙」。
 */
export async function consumeQuota(
  subject: string | Pick<DeviceClaims, "mid" | "iid">,
  chars: number
): Promise<{ ok: boolean; used: number; remaining: number; which?: "machine" | "global" }> {
  const mid = quotaSubject(subject);
  const n = Math.max(0, Math.floor(chars));
  const day = dayKey();
  const tx = db().transaction(() => {
    const cur = usedOf(day, mid);
    const gCur = usedOf(day, GLOBAL_KEY);
    const override = budgetOverrideOf(mid);
    const budget = override ?? DAILY_CHAR_BUDGET;
    if (n > 0 && cur + n > budget) {
      return { ok: false, used: cur, remaining: Math.max(0, budget - cur), which: "machine" as const };
    }
    // 显式覆写主体跳过全局熔断拒绝（用量仍计入全局账便于观测/对账）
    if (n > 0 && override === null && gCur + n > GLOBAL_DAILY_CHAR_BUDGET) {
      return { ok: false, used: cur, remaining: Math.max(0, budget - cur), which: "global" as const };
    }
    if (n > 0) {
      const up = db().prepare(
        "INSERT INTO gw_quota (day, mid, used) VALUES (?, ?, ?)" +
        " ON CONFLICT(day, mid) DO UPDATE SET used = used + excluded.used"
      );
      up.run(day, mid, n);
      up.run(day, GLOBAL_KEY, n);
    }
    const used = usedOf(day, mid);
    return { ok: true, used, remaining: Math.max(0, budget - used) };
  });
  const out = tx();
  if (Math.random() < 0.01) pruneOldDays(); // 概率式清理，避免每请求一次 DELETE
  return out;
}

/** 当日聚合（console 观测卡）：多少台机器在用、烧了多少字符、全局水位。 */
export async function gatewayDayStats(): Promise<{
  enabled: boolean;
  day: string;
  machines: number;
  chars: number;
  global_budget: number;
  machine_budget: number;
}> {
  const day = dayKey();
  let machines = 0;
  let chars = 0;
  try {
    const row = db()
      .prepare("SELECT COUNT(*) AS n, COALESCE(SUM(used),0) AS s FROM gw_quota WHERE day=? AND mid<>?")
      .get(day, GLOBAL_KEY) as { n: number; s: number } | undefined;
    machines = row?.n || 0;
    chars = row?.s || 0;
  } catch {
    /* 表还没建（零流量）→ 全 0 */
  }
  return {
    enabled: gatewayEnabled(),
    day,
    machines,
    chars,
    global_budget: GLOBAL_DAILY_CHAR_BUDGET,
    machine_budget: DAILY_CHAR_BUDGET,
  };
}

export function estimateRequestChars(body: unknown): number {
  try {
    const msgs = (body as { messages?: Array<{ content?: unknown }> })?.messages;
    if (!Array.isArray(msgs)) return 0;
    let n = 0;
    for (const m of msgs) {
      const c = m?.content;
      if (typeof c === "string") n += c.length;
      else if (Array.isArray(c)) {
        for (const part of c) {
          if (typeof part === "string") n += part.length;
          else if (part && typeof part === "object" && typeof (part as { text?: string }).text === "string") {
            n += String((part as { text: string }).text).length;
          }
        }
      }
    }
    return n;
  } catch {
    return 0;
  }
}

/** 观测流水（JSONL，绝不写消息内容——只有量级与状态）。超 10MB 轮转一份 .1。 */
export async function logGateway(rec: Record<string, unknown>): Promise<void> {
  try {
    await mkdir(path.dirname(GW_LOG), { recursive: true });
    try {
      const st = await stat(GW_LOG);
      if (st.size > GW_LOG_MAX_BYTES) {
        await rename(GW_LOG, GW_LOG + ".1"); // 覆盖旧 .1，保一代
      }
    } catch {
      /* 文件还不存在 */
    }
    await appendFile(GW_LOG, JSON.stringify({ t: new Date().toISOString(), ...rec }) + "\n");
  } catch {
    /* 观测失败不阻断主链路 */
  }
}

export async function proxyChatCompletions(
  body: Record<string, unknown>,
  signal?: AbortSignal
): Promise<Response> {
  const key = process.env.DEEPSEEK_API_KEY;
  if (!key) throw new Error("no_vendor_key");
  const payload = {
    ...body,
    model: clampModel(body.model),
    max_tokens: clampMaxTokens(body.max_tokens),
    stream: false, // 首版关流式：契约明确，非流式客户端零影响
  };
  return fetch(VENDOR_ENDPOINT, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${key}`,
    },
    body: JSON.stringify(payload),
    signal,
  });
}

export function publicGatewayBase(siteOrigin: string): string {
  return `${siteOrigin.replace(/\/+$/, "")}/api/ai/v1`;
}

export function publicModel(): string {
  return MODEL_ALLOWLIST[0];
}
