// 新人 6U 大礼包转化漏斗 —— events.jsonl（海报曝光/点击）× 订单台账（下单/支付）
// 只读聚合（实施50 P1；tail-read 模式与 trial-funnel 同构）。
//
// 口径诚实声明：
//  · 曝光/点击只含**官网侧**海报（价格页整幅 pricing_newbie_poster_* + 全站浮动
//    poster_view/click id=newbie-6u）——桌面端弹窗的曝光埋点走引擎 ui-event 通道
//    （poster6u_* 前缀，读数在引擎 /api/admin/ui-event-trend?prefix=poster6u_），
//    两套刻意不混算：跨系统凑一个「总曝光」会让分母口径没法解释；
//  · 桌面端海报带来的**下单**会落到本表（订单是全域唯一台账）——所以
//    orders/paid 是全渠道口径、views/clicks 是官网口径，卡片上分别标注；
//  · e2e 测试单（contact 含 e2e / 指纹 E2E 前缀）剔除。
import { readFile } from "fs/promises";
import path from "path";
import { ANALYTICS_DIR } from "./data-dir";
import { listOrders } from "./order-store";
import { NEWBIE_PACK } from "./chatx-pricing";

const LOG = process.env.ANALYTICS_LOG || path.join(ANALYTICS_DIR, "events.jsonl");
const MAX_LINES = 100_000;

const VIEW_EVENTS = new Set(["poster_view", "pricing_newbie_poster_view"]);
const CLICK_EVENTS = new Set(["poster_click", "pricing_newbie_poster_cta", "pricing_newbie_cta"]);

export interface NewbieFunnelWindow {
  days: number;
  /** 官网海报曝光（会话去重 + 匿名按次） */
  poster_views: number;
  /** 官网海报点击 */
  poster_clicks: number;
  /** 新人包订单：创建 / 已付（全渠道，剔 e2e 测试单） */
  orders_created: number;
  orders_paid: number;
  /** 其中桌面弹窗海报引流（订单 utm_source === "chatx_desktop"；下单页直落即记 +
   *  7 天 localStorage 续存，「点海报→逛两天→回来下单」仍归因得到）。归因字段
   *  2026-08-22 起随单落库——之前的老订单无此字段，一律计入「官网/自然」侧，
   *  卡片脚注如实声明，绝不追溯脑补。 */
  orders_created_desktop: number;
  orders_paid_desktop: number;
  /** 官网点击 → 已付转化率（%；分母为 0 时 null） */
  click_to_paid: number | null;
}

function isTestOrder(o: { contact?: string; fingerprint?: string }): boolean {
  return /e2e/i.test(String(o.contact || "")) ||
    String(o.fingerprint || "").toUpperCase().startsWith("E2E");
}

export async function newbieFunnel(days: number): Promise<NewbieFunnelWindow> {
  const d = Math.min(90, Math.max(1, Math.floor(days) || 7));
  const since = Date.now() - d * 86_400_000;

  const viewSids = new Set<string>();
  const clickSids = new Set<string>();
  let viewAnon = 0;
  let clickAnon = 0;
  let raw = "";
  try {
    raw = await readFile(LOG, "utf8");
  } catch {
    /* 尚无事件文件 → 曝光/点击全 0 */
  }
  const lines = raw.split("\n");
  for (const line of lines.length > MAX_LINES ? lines.slice(-MAX_LINES) : lines) {
    if (!line || (!line.includes("poster_") && !line.includes("pricing_newbie"))) continue;
    try {
      const r = JSON.parse(line) as {
        t?: string; event?: string; sid?: string; props?: { id?: string };
      };
      if (!r.t || Date.parse(r.t) < since) continue;
      const ev = String(r.event || "");
      // 全站浮动海报事件带 props.id 区分活动；价格页整幅海报事件名自带 newbie 语义
      const isNewbie = ev.startsWith("pricing_newbie") || (r.props?.id === "newbie-6u");
      if (!isNewbie) continue;
      if (VIEW_EVENTS.has(ev)) {
        if (r.sid) viewSids.add(r.sid);
        else viewAnon += 1;
      } else if (CLICK_EVENTS.has(ev)) {
        if (r.sid) clickSids.add(r.sid);
        else clickAnon += 1;
      }
    } catch {
      /* 追加式 jsonl 可能留半行，跳过 */
    }
  }

  let created = 0;
  let paid = 0;
  let createdDesktop = 0;
  let paidDesktop = 0;
  try {
    const orders = await listOrders();
    for (const o of orders) {
      const skuHit = o.sku_id === NEWBIE_PACK.skuId || o.plan === NEWBIE_PACK.key;
      if (!skuHit || isTestOrder(o)) continue;
      const t = Date.parse(o.t || "");
      if (!t || t < since) continue;
      const fromDesktop = o.utm_source === "chatx_desktop";
      created += 1;
      if (fromDesktop) createdDesktop += 1;
      // 状态判定而非 paid_at 残留：退款单（保留 paid_at）不算转化
      if (o.status === "paid" || o.status === "activated") {
        paid += 1;
        if (fromDesktop) paidDesktop += 1;
      }
    }
  } catch {
    /* 订单台账读取失败 → 订单段 0（曝光段照常） */
  }

  const clicks = clickSids.size + clickAnon;
  return {
    days: d,
    poster_views: viewSids.size + viewAnon,
    poster_clicks: clicks,
    orders_created: created,
    orders_paid: paid,
    orders_created_desktop: createdDesktop,
    orders_paid_desktop: paidDesktop,
    click_to_paid: clicks > 0 ? Math.round((paid / clicks) * 1000) / 10 : null,
  };
}
