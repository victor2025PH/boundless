import { mkdir, readFile, rename, writeFile } from "fs/promises";
import path from "path";
import { DATA_DIR } from "./data-dir";

// 支付渠道设置存储：payment-settings.json（原子 tmp+rename 写 + 单进程串行化，
// 与 order-store 同款模式）。文件里只存非机密配置；Stripe Secret Key 只从环境变量
// STRIPE_SECRET_KEY 读取——绝不写入 JSON、绝不下发浏览器。

const FILE = path.join(DATA_DIR, "payment-settings.json");

export interface PaymentSettings {
  usdt: { enabled: boolean; address: string };
  card: {
    enabled: boolean;
    provider: "stripe";
    /** Stripe publishable key（pk_…，公开无害，可存文件/下发前端）。 */
    publishableKey: string;
    /** ISO 货币码，默认 USD。 */
    currency: string;
    /** 支付成功回跳；留空则结算时回落到 SITE_URL/order?check=<单号>。 */
    successUrl: string;
    /** 取消支付回跳；留空则结算时回落到 SITE_URL/order。 */
    cancelUrl: string;
  };
  updatedAt: string;
}

/** 保存补丁：usdt/card 子对象按字段浅合并（provider 恒为 stripe）。 */
export type PaymentSettingsPatch = {
  usdt?: Partial<PaymentSettings["usdt"]>;
  card?: Partial<PaymentSettings["card"]>;
};

function defaults(): PaymentSettings {
  return {
    usdt: { enabled: true, address: process.env.NEXT_PUBLIC_USDT_ADDR || "" },
    card: {
      enabled: false,
      provider: "stripe",
      publishableKey: "",
      currency: "USD",
      successUrl: "",
      cancelUrl: "",
    },
    updatedAt: "",
  };
}

let chain: Promise<unknown> = Promise.resolve();
function serialize<T>(fn: () => Promise<T>): Promise<T> {
  const next = chain.then(fn, fn);
  chain = next.catch(() => {});
  return next;
}

const bool = (v: unknown, d: boolean) => (typeof v === "boolean" ? v : d);
const str = (v: unknown, d: string, max: number) => (typeof v === "string" ? v.slice(0, max) : d);

/** 宽松形状：文件可能被手改/损坏，字段逐个校验类型后才采纳（否则用兜底值）。 */
type LoosePatch = {
  usdt?: { enabled?: unknown; address?: unknown } | null;
  card?: {
    enabled?: unknown;
    publishableKey?: unknown;
    currency?: unknown;
    successUrl?: unknown;
    cancelUrl?: unknown;
  } | null;
  updatedAt?: unknown;
};

function merge(base: PaymentSettings, patch: LoosePatch): PaymentSettings {
  return {
    usdt: {
      enabled: bool(patch.usdt?.enabled, base.usdt.enabled),
      address: str(patch.usdt?.address, base.usdt.address, 120),
    },
    card: {
      enabled: bool(patch.card?.enabled, base.card.enabled),
      provider: "stripe",
      publishableKey: str(patch.card?.publishableKey, base.card.publishableKey, 200),
      currency: str(patch.card?.currency, base.card.currency, 10) || "USD",
      successUrl: str(patch.card?.successUrl, base.card.successUrl, 300),
      cancelUrl: str(patch.card?.cancelUrl, base.card.cancelUrl, 300),
    },
    updatedAt: str(patch.updatedAt, base.updatedAt, 40),
  };
}

async function readSettings(): Promise<PaymentSettings> {
  try {
    const parsed = JSON.parse(await readFile(FILE, "utf-8"));
    if (parsed && typeof parsed === "object") return merge(defaults(), parsed as LoosePatch);
  } catch {
    /* first run — defaults below */
  }
  return defaults();
}

export async function getPaymentSettings(): Promise<PaymentSettings> {
  return readSettings();
}

export async function savePaymentSettings(patch: PaymentSettingsPatch): Promise<PaymentSettings> {
  return serialize(async () => {
    const next = merge(await readSettings(), { ...patch, updatedAt: new Date().toISOString() });
    await mkdir(path.dirname(FILE), { recursive: true });
    const tmp = FILE + ".tmp";
    await writeFile(tmp, JSON.stringify(next, null, 2));
    await rename(tmp, FILE);
    return next;
  });
}

/** 浏览器可见的非机密子集：结算弹窗读它决定展示哪些支付方式。 */
export interface PublicPaymentSettings {
  usdt: { enabled: boolean; address: string };
  card: { enabled: boolean; provider: "stripe"; publishableKey: string; currency: string };
  /** Stripe Secret 是否已配在服务器环境变量（只回布尔，永不回密钥本身）。 */
  cardSecretConfigured: boolean;
}

export async function getPublicPaymentSettings(): Promise<PublicPaymentSettings> {
  const s = await readSettings();
  return {
    usdt: { enabled: s.usdt.enabled, address: s.usdt.address },
    card: {
      enabled: s.card.enabled,
      provider: s.card.provider,
      publishableKey: s.card.publishableKey,
      currency: s.card.currency,
    },
    cardSecretConfigured: !!process.env.STRIPE_SECRET_KEY,
  };
}

/** Stripe Secret Key：只从环境变量读，绝不落盘、绝不下发。 */
export function stripeSecret(): string {
  return process.env.STRIPE_SECRET_KEY || "";
}
