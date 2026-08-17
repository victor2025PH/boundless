import { appendFile, mkdir, readFile, rename, writeFile } from "fs/promises";
import path from "path";
import { getAdminChats } from "./admin-store";
import { DATA_DIR } from "./data-dir";
import { resolveOrderSku } from "./offer-map";

// 客户端订阅/授权订单存储：orders-db.json（键控 + 状态机）+ orders.jsonl（追加审计流水）。
// 与 lead-store 同款的原子写 + 单进程串行化模式。

const DB = process.env.ORDERS_DB || path.join(DATA_DIR, "orders-db.json");
const LOG = process.env.ORDERS_LOG || path.join(DATA_DIR, "orders.jsonl");

/** pending 待付款 → paid 已到账 → activated 已开通；cancelled 取消。 */
export type OrderStatus = "pending" | "paid" | "activated" | "cancelled";
export const ORDER_STATUSES: OrderStatus[] = ["pending", "paid", "activated", "cancelled"];

export interface OrderEntry {
  id: string;
  t: string;
  status: OrderStatus;
  plan: string;
  edition: string;
  period: string;
  /** 全域 SKU 关联键，见 platform/licensing/sku_registry.json（下单时经 lib/offer-map.ts
   *  的 resolveOrderSku 推断填充；映射不到则不写，宁缺毋错）。 */
  sku_id?: string;
  /** 全域产品 id（zhituo/zhiliao/tongyi/…），随 sku_id 一起推断填充。 */
  product_id?: string;
  /** 挂牌金额（整数 USDT）。 */
  amount: number;
  /** 实际应付金额 = amount + 唯一小数尾数：到账金额可反查订单，自动核销的关键。
   *  卡支付（Stripe）按 session 对账不需要尾数，pay_amount === amount。 */
  pay_amount: number;
  currency: "USDT" | "USD";
  /** 支付方式：usdt 链上转账（默认；历史订单无此字段视同 usdt）/ card 银行卡（Stripe Checkout）。 */
  method?: "usdt" | "card";
  /** Stripe Checkout Session id（卡支付审计/对账用，webhook 落地后回填）。 */
  stripe_session_id?: string;
  contact: string;
  fingerprint: string;
  lang: string;
  ip?: string;
  ua?: string;
  paid_at?: string;
  activated_at?: string;
  /** 履约兑换码：由厂商机（私钥本地）签发后回填；开通后在状态页展示给客户。 */
  code?: string;
  /** 客户 Telegram chat_id：客户点 /order 上的「接收开通通知」深链绑定，开通/到账即自动私信。 */
  notify_chat?: string;
  /** 已发过到账/开通/到期私信的去重标记（避免巡检重复打扰）。 */
  notified?: { paid?: boolean; activated?: boolean; expiring?: boolean };
  /** 已到账超时未开通的兜底告警标记（履约机疑似离线，只提醒管理员一次）。 */
  sla_alerted?: boolean;
}

interface OrderDb {
  version: 1;
  orders: Record<string, OrderEntry>;
}

let chain: Promise<unknown> = Promise.resolve();
function serialize<T>(fn: () => Promise<T>): Promise<T> {
  const next = chain.then(fn, fn);
  chain = next.catch(() => {});
  return next;
}

async function readDb(): Promise<OrderDb> {
  try {
    const parsed = JSON.parse(await readFile(DB, "utf-8"));
    if (parsed?.orders) return parsed as OrderDb;
  } catch {
    /* first run */
  }
  return { version: 1, orders: {} };
}

async function writeDb(db: OrderDb) {
  await mkdir(path.dirname(DB), { recursive: true });
  const tmp = DB + ".tmp";
  await writeFile(tmp, JSON.stringify(db));
  await rename(tmp, DB);
}

function newOrderId(): string {
  const d = new Date();
  const ymd = `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}`;
  const rand = Math.random().toString(36).slice(2, 8).toUpperCase();
  return `AH-${ymd}-${rand}`;
}

/** 给订单分配唯一付款尾数（0.01–0.99），避开所有未完结订单已占用的尾数。
 *  到账金额 = 挂牌价 + 尾数，链上金额即可唯一定位订单（自动核销基础）。 */
function allocPayAmount(db: OrderDb, amount: number): number {
  if (amount <= 0) return 0;
  const used = new Set(
    Object.values(db.orders)
      .filter((o) => o.status === "pending")
      .map((o) => Math.round((o.pay_amount % 1) * 100))
  );
  const free: number[] = [];
  for (let c = 1; c <= 99; c++) if (!used.has(c)) free.push(c);
  const cents = free.length ? free[Math.floor(Math.random() * free.length)] : Math.floor(Math.random() * 99) + 1;
  return Math.round((amount + cents / 100) * 100) / 100;
}

export async function createOrder(
  input: Omit<OrderEntry, "id" | "t" | "status" | "pay_amount" | "currency">
): Promise<OrderEntry> {
  return serialize(async () => {
    const db = await readDb();
    // usdt：分配唯一小数尾数供链上自动核销；card：Stripe 按 session 对账，金额原样不加尾数。
    const method: "usdt" | "card" = input.method === "card" ? "card" : "usdt";
    const entry: OrderEntry = {
      ...input,
      id: newOrderId(),
      t: new Date().toISOString(),
      status: "pending",
      method,
      pay_amount: method === "card" ? input.amount : allocPayAmount(db, input.amount),
      currency: method === "card" ? "USD" : "USDT",
    };
    // 出生即带全域 SKU 关联（sku_registry.json）；映射为 null 时不写字段，宁缺毋错。
    const sku = resolveOrderSku(input.plan, input.edition, input.period);
    if (sku.skuId) entry.sku_id = sku.skuId;
    if (sku.productId) entry.product_id = sku.productId;
    db.orders[entry.id] = entry;
    await writeDb(db);
    await appendFile(LOG, JSON.stringify(entry) + "\n", "utf-8").catch(() => {});
    // 影子账本双写（best-effort，不 await、失败静默，绝不影响下单主链路）
    void import("./ledger-sync").then((m) => m.syncOrderEntry(entry)).catch(() => {});
    return entry;
  });
}

export async function getOrder(id: string): Promise<OrderEntry | null> {
  const db = await readDb();
  return db.orders[id.trim().toUpperCase()] ?? null;
}

/** 客户绑定 Telegram 接收通知：/start <订单号> 命中时写入 notify_chat。返回订单或 null。 */
export async function bindOrderNotify(id: string, chatId: string | number): Promise<OrderEntry | null> {
  return serialize(async () => {
    const db = await readDb();
    const o = db.orders[id.trim().toUpperCase()];
    if (!o) return null;
    o.notify_chat = String(chatId);
    await writeDb(db);
    // 影子账本双写（best-effort，失败静默）
    void import("./ledger-sync").then((m) => m.syncOrderEntry(o)).catch(() => {});
    return o;
  });
}

export async function setOrderStatus(id: string, status: OrderStatus, code?: string): Promise<OrderEntry | null> {
  return serialize(async () => {
    const db = await readDb();
    const o = db.orders[id.trim().toUpperCase()];
    if (!o) return null;
    o.status = status;
    if (status === "paid" && !o.paid_at) o.paid_at = new Date().toISOString();
    if (status === "activated" && !o.activated_at) o.activated_at = new Date().toISOString();
    if (code) o.code = code.slice(0, 4000); // 兑换码（短）或整份签名授权的 base64（长）
    await writeDb(db);
    await appendFile(LOG, JSON.stringify({ t: new Date().toISOString(), id: o.id, event: `status:${status}` }) + "\n", "utf-8").catch(() => {});
    // 影子账本双写（best-effort，失败静默）
    void import("./ledger-sync").then((m) => m.syncOrderEntry(o)).catch(() => {});
    return o;
  });
}

/** Stripe webhook 对账落地：标记到账 + 回填 session id，一次串行写完成（幂等）。
 *  返回 changed=false 表示订单已是 paid/activated（Stripe 会重试投递、同事件可能多次到达，
 *  调用方据此跳过重复通知）。 */
export async function markOrderCardPaid(
  id: string,
  sessionId: string
): Promise<{ order: OrderEntry; changed: boolean } | null> {
  return serialize(async () => {
    const db = await readDb();
    const o = db.orders[id.trim().toUpperCase()];
    if (!o) return null;
    if (o.status === "paid" || o.status === "activated") {
      // 已到账/已开通：只补 session id（若缺），不动状态、不重复记流水
      if (!o.stripe_session_id && sessionId) {
        o.stripe_session_id = sessionId.slice(0, 120);
        await writeDb(db);
      }
      return { order: o, changed: false };
    }
    o.status = "paid";
    o.paid_at = o.paid_at || new Date().toISOString();
    if (sessionId) o.stripe_session_id = sessionId.slice(0, 120);
    await writeDb(db);
    await appendFile(
      LOG,
      JSON.stringify({ t: new Date().toISOString(), id: o.id, event: "status:paid", via: "stripe_webhook" }) + "\n",
      "utf-8"
    ).catch(() => {});
    // 影子账本双写（best-effort，失败静默）
    void import("./ledger-sync").then((m) => m.syncOrderEntry(o)).catch(() => {});
    return { order: o, changed: true };
  });
}

export async function listPendingOrders(): Promise<OrderEntry[]> {
  const db = await readDb();
  return Object.values(db.orders).filter((o) => o.status === "pending");
}

/** 全量/按状态列订单（新→旧），管理员接口用。 */
export async function listOrders(status?: string): Promise<OrderEntry[]> {
  const db = await readDb();
  const all = Object.values(db.orders).sort((a, b) => (a.t < b.t ? 1 : -1));
  return status ? all.filter((o) => o.status === status) : all;
}

const PERIOD_SUB_DAYS: Record<string, number> = { monthly: 30, annual: 365 };

/** SLA/续费巡检（服务器 cron 每 10 分钟经 /api/admin/order-sla 调用；用官网自身原子存储，无多进程竞态）：
 *  ① 已到账超时未开通 → 疑似履约机离线，告警管理员（带一键开通）；
 *  ② 已开通订阅临期（默认 3 天内）→ 私信客户续费 + 提醒管理员。两类都用 flag 去重，不重复打扰。 */
export async function runOrderSla(): Promise<{ stale: number; expiring: number }> {
  const stalePaidMin = Number(process.env.ORDER_STALE_PAID_MIN || 15);
  const warnDays = Number(process.env.ORDER_EXPIRE_WARN_DAYS || 3);
  const now = Date.now();
  const stale: OrderEntry[] = [];
  const expiring: { o: OrderEntry; daysLeft: number }[] = [];

  await serialize(async () => {
    const db = await readDb();
    let dirty = false;
    for (const o of Object.values(db.orders)) {
      if (o.status === "paid" && !o.sla_alerted && o.paid_at) {
        if (now - new Date(o.paid_at).getTime() > stalePaidMin * 60000) {
          o.sla_alerted = true;
          dirty = true;
          stale.push(o);
        }
      }
      if (o.status === "activated" && o.activated_at && !o.notified?.expiring) {
        const days = PERIOD_SUB_DAYS[o.period] || 0;
        if (days > 0) {
          const leftDays = Math.ceil((new Date(o.activated_at).getTime() + days * 86400000 - now) / 86400000);
          if (leftDays > 0 && leftDays <= warnDays) {
            o.notified = { ...(o.notified || {}), expiring: true };
            dirty = true;
            expiring.push({ o, daysLeft: leftDays });
          }
        }
      }
    }
    if (dirty) await writeDb(db);
  });

  // 网络发送放在锁外，不阻塞串行写链
  const site = process.env.NEXT_PUBLIC_SITE_URL || "https://bd2026.cc";
  const key = process.env.ADMIN_KEY || process.env.TELEGRAM_SETUP_KEY || "";
  for (const o of stale) {
    const mark = `${site}/api/admin/order-status?id=${encodeURIComponent(o.id)}&status=activated&key=${encodeURIComponent(key)}`;
    await notifyAdmins(
      `⚠️ <b>订单已到账 ${stalePaidMin} 分钟仍未开通</b>\n<code>${o.id}</code> · ${o.plan} · 应付 ${o.pay_amount} USDT\n联系：${o.contact}\n` +
        (o.fingerprint ? "" : "（缺机器指纹，需联系客户补）\n") +
        "履约机可能离线，请检查或手动开通。",
      key ? [[{ text: "🚀 手动标记已开通", url: mark }]] : undefined
    );
  }
  for (const { o, daysLeft } of expiring) {
    await notifyCustomerOfStatus(o, "expiring", daysLeft);
    await notifyAdmins(
      `⏰ 订单 <code>${o.id}</code>（${o.plan}）约 ${daysLeft} 天后到期，` +
        (o.notify_chat ? "已提醒客户续费。" : "客户未绑定 TG，建议主动联系。") +
        `\n联系：${o.contact}`
    );
  }
  return { stale: stale.length, expiring: expiring.length };
}

function tgToken(): string {
  return process.env.TELEGRAM_BOT_TOKEN || "";
}

async function tgSend(chatId: string | number, text: string, inlineKeyboard?: unknown) {
  const token = tgToken();
  if (!token) return;
  await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      chat_id: chatId,
      text,
      parse_mode: "HTML",
      disable_web_page_preview: true,
      reply_markup: inlineKeyboard ? { inline_keyboard: inlineKeyboard } : undefined,
    }),
  }).catch(() => {});
}

/** 给所有管理员群发一段纯文本（SLA 告警等）。 */
export async function notifyAdmins(text: string, inlineKeyboard?: unknown) {
  const chats = await getAdminChats();
  await Promise.allSettled(chats.map((c) => tgSend(c, text, inlineKeyboard)));
}

const zhPlan = (o: OrderEntry) => `${o.plan}${o.period === "annual" ? " · 年付" : o.period === "monthly" ? " · 月付" : ""}`;

/** 到账/开通/临期时自动私信已绑定的客户（notify_chat）。客户没绑定则静默跳过（仍可自助查询）。 */
export async function notifyCustomerOfStatus(o: OrderEntry, kind: "paid" | "activated" | "expiring", daysLeft?: number) {
  if (!o.notify_chat) return;
  const site = process.env.NEXT_PUBLIC_SITE_URL || "https://bd2026.cc";
  const checkUrl = `${site}/order?check=${encodeURIComponent(o.id)}`;
  if (kind === "paid") {
    await tgSend(
      o.notify_chat,
      `✅ <b>已收到你的付款！</b>\n订单 <code>${o.id}</code>（${zhPlan(o)}）正在为你自动开通，通常几分钟内完成。开通后我会在这里把授权码发给你。`,
      [[{ text: "📄 查看订单进度", url: checkUrl }]]
    );
  } else if (kind === "activated") {
    await tgSend(
      o.notify_chat,
      `🎉 <b>订单已开通！</b>\n<code>${o.id}</code> · ${zhPlan(o)}\n\n` +
        `最快激活方式：打开客户端 →「🔑 授权」→ 在「订单号」框输入 <code>${o.id}</code> → 点「在线激活」即可生效。\n` +
        `（备用：订单页可复制完整授权码手动粘贴激活。）`,
      [[{ text: "📄 打开订单页", url: checkUrl }]]
    );
  } else if (kind === "expiring") {
    await tgSend(
      o.notify_chat,
      `⏰ <b>订阅即将到期</b>\n订单 <code>${o.id}</code>（${zhPlan(o)}）将在约 ${daysLeft ?? 3} 天后到期。点下方续费保持不中断。`,
      [[{ text: "🔄 立即续费", url: `${site}/order?plan=${encodeURIComponent(o.plan)}&period=${o.period}` }]]
    );
  }
}

/** 管理员 Telegram 通知：订单详情 + 一键改状态链接（走 requireAdmin 的 query key 通道）。 */
export async function notifyAdminsOfOrder(o: OrderEntry) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) return;
  const chats = await getAdminChats();
  if (!chats.length) return;
  const site = process.env.NEXT_PUBLIC_SITE_URL || "https://bd2026.cc";
  const key = process.env.ADMIN_KEY || process.env.TELEGRAM_SETUP_KEY || "";
  const mark = (s: OrderStatus) =>
    `${site}/api/admin/order-status?id=${encodeURIComponent(o.id)}&status=${s}&key=${encodeURIComponent(key)}`;
  const text =
    `🧾 新订单 ${o.id}\n` +
    `套餐：${o.plan} (${o.edition}) · ${o.period}\n` +
    `应付：${o.pay_amount} USDT（挂牌 ${o.amount} + 识别尾数）\n` +
    `联系：${o.contact}\n` +
    (o.fingerprint ? `指纹：${o.fingerprint}\n` : "") +
    `状态：待付款`;
  const body = {
    text,
    reply_markup: key
      ? {
          inline_keyboard: [
            [
              { text: "✅ 标记已到账", url: mark("paid") },
              { text: "🚀 标记已开通", url: mark("activated") },
            ],
            [{ text: "❌ 取消订单", url: mark("cancelled") }],
          ],
        }
      : undefined,
  };
  await Promise.allSettled(
    chats.map((chat_id) =>
      fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id, ...body }),
      })
    )
  );
}
