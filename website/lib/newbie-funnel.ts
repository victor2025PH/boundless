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
  try {
    const orders = await listOrders();
    for (const o of orders) {
      const skuHit = o.sku_id === NEWBIE_PACK.skuId || o.plan === NEWBIE_PACK.key;
      if (!skuHit || isTestOrder(o)) continue;
      const t = Date.parse(o.t || "");
      if (!t || t < since) continue;
      created += 1;
      // 状态判定而非 paid_at 残留：退款单（保留 paid_at）不算转化
      if (o.status === "paid" || o.status === "activated") paid += 1;
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
    click_to_paid: clicks > 0 ? Math.round((paid / clicks) * 1000) / 10 : null,
  };
}
