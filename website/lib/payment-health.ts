// 支付对账健康读数 —— /console「支付对账健康」卡的数据源。
// 聚合 events.jsonl 里的 stripe_reconcile_run 系统事件（由 /api/admin/stripe-reconcile
// 每次运行后写入，见 lib/server-events.ts），回答三件事：
//   巡检有没有在跑（最近一次何时）/ 双通道有没有漏单（settled>0 = webhook 漏了被巡检捞回）
//   / 有没有需要人工的异常（金额不符、孤儿 session）。
import { readFile } from "fs/promises";
import path from "path";
import { ANALYTICS_DIR } from "./data-dir";

const LOG = process.env.ANALYTICS_LOG || path.join(ANALYTICS_DIR, "events.jsonl");
const MAX_LINES = 100_000;

export interface ReconcileRun {
  t: string;
  windowHours: number;
  sessionsChecked: number;
  paidSessions: number;
  settled: number;
  already: number;
  amount_mismatch: number;
  order_not_found: number;
  foreign: number;
  recovered: string[];
}

export interface ReconcileHealth {
  days: number;
  runs: number;
  lastRun: ReconcileRun | null;
  /** 距最近一次运行的分钟数（无运行为 null）；>26h 说明 cron 掉了 */
  lastAgoMin: number | null;
  totals: { settled: number; already: number; amount_mismatch: number; order_not_found: number };
  /** 近 N 天被巡检补账的订单号（webhook 漏单证据），最多 20 个 */
  recovered: string[];
}

export async function readReconcileHealth(days: number): Promise<ReconcileHealth> {
  const d = Math.min(90, Math.max(1, Math.floor(days) || 7));
  const since = Date.now() - d * 86_400_000;

  let raw = "";
  try {
    raw = await readFile(LOG, "utf8");
  } catch {
    /* 尚无事件文件 */
  }

  const runs: ReconcileRun[] = [];
  const lines = raw.split("\n");
  for (const line of lines.length > MAX_LINES ? lines.slice(-MAX_LINES) : lines) {
    if (!line || !line.includes('"stripe_reconcile_run"')) continue;
    try {
      const r = JSON.parse(line) as { t?: string; event?: string; props?: Partial<ReconcileRun> | null };
      if (r.event !== "stripe_reconcile_run" || !r.t || Date.parse(r.t) < since) continue;
      const p = r.props ?? {};
      runs.push({
        t: r.t,
        windowHours: Number(p.windowHours ?? 0),
        sessionsChecked: Number(p.sessionsChecked ?? 0),
        paidSessions: Number(p.paidSessions ?? 0),
        settled: Number(p.settled ?? 0),
        already: Number(p.already ?? 0),
        amount_mismatch: Number(p.amount_mismatch ?? 0),
        order_not_found: Number(p.order_not_found ?? 0),
        foreign: Number(p.foreign ?? 0),
        recovered: Array.isArray(p.recovered) ? p.recovered.map(String) : [],
      });
    } catch {
      /* 跳过坏行 */
    }
  }

  runs.sort((a, b) => (a.t < b.t ? -1 : 1));
  const last = runs[runs.length - 1] ?? null;
  const totals = { settled: 0, already: 0, amount_mismatch: 0, order_not_found: 0 };
  const recovered: string[] = [];
  for (const r of runs) {
    totals.settled += r.settled;
    totals.already += r.already;
    totals.amount_mismatch += r.amount_mismatch;
    totals.order_not_found += r.order_not_found;
    recovered.push(...r.recovered);
  }

  return {
    days: d,
    runs: runs.length,
    lastRun: last,
    lastAgoMin: last ? Math.round((Date.now() - Date.parse(last.t)) / 60_000) : null,
    totals,
    recovered: recovered.slice(-20),
  };
}

/* ================= Stripe 上线就绪清单 =================
 * 控制台「支付对账健康」在三灯未全绿时展示下一步；不回显任何密钥值。 */

export type StripeSetupStepId =
  | "secret"
  | "publishable"
  | "webhook_secret"
  | "card_enabled"
  | "cron_fresh";

export interface StripeSetupStep {
  id: StripeSetupStepId;
  ok: boolean;
  label: string;
  /** 未完成时的可执行提示 */
  hint: string;
}

export interface StripeSetupStatus {
  ready: boolean;
  done: number;
  total: number;
  steps: StripeSetupStep[];
}

export async function getStripeSetupStatus(opts?: {
  /** 距最近一次 reconcile 的分钟数；null = 尚无记录 */
  lastAgoMin?: number | null;
}): Promise<StripeSetupStatus> {
  const { getPaymentSettings } = await import("./payment-settings");
  const settings = await getPaymentSettings();
  const secretOk = !!(process.env.STRIPE_SECRET_KEY || "").trim();
  const webhookOk = !!(process.env.STRIPE_WEBHOOK_SECRET || "").trim();
  const pkOk = !!(settings.card.publishableKey || "").trim();
  const cardOk = !!settings.card.enabled;
  // cron 新鲜度：有记录且 <26h 算绿；尚无记录不阻塞「配置完成」（未配 Stripe 时空转无害）
  const ago = opts?.lastAgoMin;
  const cronOk = ago == null ? true : ago <= 26 * 60;

  const steps: StripeSetupStep[] = [
    {
      id: "secret",
      ok: secretOk,
      label: "STRIPE_SECRET_KEY",
      hint: "在服务器 ~/yuntech/.env.local 写入 sk_live_…（或 sk_test_…）后 pm2 restart yuntech",
    },
    {
      id: "publishable",
      ok: pkOk,
      label: "Publishable Key",
      hint: "在 /admin/payment 填写 pk_… 并保存",
    },
    {
      id: "webhook_secret",
      ok: webhookOk,
      label: "STRIPE_WEBHOOK_SECRET",
      hint: "Stripe Dashboard → Webhooks → 端点 https://bd2026.cc/api/payment/webhook（事件 checkout.session.completed）→ 复制 whsec_… 写入 .env.local 并重启",
    },
    {
      id: "card_enabled",
      ok: cardOk,
      label: "后台启用卡支付",
      hint: "在 /admin/payment 打开「银行卡 / Stripe」开关",
    },
    {
      id: "cron_fresh",
      ok: cronOk,
      label: "日巡检 cron",
      hint: "检查 crontab 是否有 stripe-reconcile（每日 04:10）；或手动 curl /api/admin/stripe-reconcile",
    },
  ];

  // ready = 支付真正可接单（密钥+公钥+webhook+开关）；cron 单独告警不挡 ready
  const configSteps = steps.filter((s) => s.id !== "cron_fresh");
  const done = configSteps.filter((s) => s.ok).length;
  return {
    ready: configSteps.every((s) => s.ok),
    done,
    total: configSteps.length,
    steps,
  };
}
