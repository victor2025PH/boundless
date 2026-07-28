// 客服核销直通车：官网 bot（webhook）里识别 BC-XXXX-XXXX 绑定码并核销。
//
// 为什么长在 webhook 里而不是独立 bot 进程：TELEGRAM_BOT_TOKEN 已经以 webhook 模式
// 服务官网（getUpdates 会与之冲突），而核销的台账（trial-claim-store）就在本进程——
// 直接调 redeemBindCode 连 console 登录都不需要，少一套凭证、少一个常驻进程。
//
// 保守自动化（人在环）：只有 admin_chats（/bindadmin 口令绑定的管理员/客服会话）发的
// 码才核销；客户自己把码发给 bot → 不核销，转交管理员并回「已转交客服」。每日封顶
// （进程内计数，重启归零；每笔核销都进 trial-claims 审计流水，账不会丢）。
import { redeemBindCode } from "./trial-claim-store";

const BC_RE = /\bBC-([0-9A-Z]{4})-([0-9A-Z]{4})\b/gi;

export const CS_REDEEM_DAILY_CAP = Number(process.env.CS_REDEEM_DAILY_CAP || 100);
const GIFT_CHARS = Number(process.env.TRIAL_GIFT_CHARS || 100000);

/** 消息文本 → 绑定码列表（大写规整 + 保序去重）。 */
export function extractBindCodes(text: string): string[] {
  const out: string[] = [];
  for (const m of String(text || "").matchAll(BC_RE)) {
    const code = `BC-${m[1].toUpperCase()}-${m[2].toUpperCase()}`;
    if (!out.includes(code)) out.push(code);
  }
  return out;
}

const dailyCount = new Map<string, number>();

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

function capReached(): boolean {
  return (dailyCount.get(today()) ?? 0) >= CS_REDEEM_DAILY_CAP;
}

function bumpCount(): void {
  const d = today();
  dailyCount.set(d, (dailyCount.get(d) ?? 0) + 1);
  // 只留今天的键，别让长驻进程慢慢攒一堆历史日期
  for (const k of dailyCount.keys()) if (k !== d) dailyCount.delete(k);
}

/** 管理员会话发码 → 逐个核销，返回给客服看的回执（每行一个码）。 */
export async function redeemForAdmin(codes: string[], chatId: number): Promise<string> {
  const lines: string[] = [];
  for (const code of codes) {
    if (capReached()) {
      lines.push(`⏸️ 已达今日核销上限（${CS_REDEEM_DAILY_CAP}），${code} 未处理——明天再发，或登录 /console/trial 手动核销`);
      continue;
    }
    const r = await redeemBindCode(code, { chars: GIFT_CHARS, by: `tg-bot:${chatId}` });
    if (!r.ok) {
      lines.push(
        r.reason === "not_found"
          ? `❌ ${code} 不存在——让客户核对有没有抄错（客户端「会员中心」能重新看码）`
          : `❌ ${code} 格式不对`
      );
      continue;
    }
    if (r.alreadyRedeemed) {
      // 幂等重复：不重复入账，也不占当日预算
      lines.push(`⚠️ ${code} 之前已核销过（不会重复入账）。联系方式：${r.claim.contact || "—"}`);
      continue;
    }
    bumpCount();
    const chars = r.claim.bindChars || GIFT_CHARS;
    const tail = r.claim.topupVoucher ? "" : "，厂商机签发后自动到账";
    lines.push(`✅ 已核销 ${code} → ${r.claim.contact || "—"} +${chars.toLocaleString()} 字符${tail}`);
  }
  return lines.join("\n");
}

/** 客户（非管理员）发码时给 TA 的回复 + 转交管理员的通知文案。 */
export function customerHandoffTexts(
  codes: string[],
  who: string,
  lang: "zh" | "en"
): { reply: string; notify: string } {
  const list = codes.join(" / ");
  const reply =
    lang === "zh"
      ? `📨 已把你的领取码 ${list} 转交客服，核销后额度会自动到账，不用你再操作。`
      : `📨 Your code ${list} has been forwarded to support. Once redeemed, the quota lands automatically — nothing else to do.`;
  const notify =
    `🎁 <b>客户发来赠量绑定码</b>：<code>${list}</code>\n来自：${who}\n` +
    `直接把码回发给本 bot 即可核销（或到 /console/trial 手动核）。`;
  return { reply, notify };
}
