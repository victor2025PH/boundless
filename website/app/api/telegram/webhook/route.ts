import { NextRequest, NextResponse } from "next/server";
import { detectLang } from "@/lib/bot-knowledge";
import {
  answerCallback,
  handleCallback,
  handleCommand,
  handleFreeText,
  handleGroupMessage,
  sendDocument,
  sendText,
} from "@/lib/telegram-bot";
import { bindAdminChat, getAdminChats, unbindAdminChat } from "@/lib/admin-store";
import { bindOrderNotify, notifyAdmins } from "@/lib/order-store";
import { bindTelegram, collectPearlByTg, parseStartToken, redeemDragonCode, setRemindByTg, type DragonState } from "@/lib/dragon-store";
import { isDuplicateUpdate } from "@/lib/tg-dedup";
import { BOT_HANDLE, TELEGRAM_GROUP, TELEGRAM_DISPLAY, SITE_URL } from "@/lib/site";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type TgUpdate = {
  update_id?: number;
  message?: {
    message_id?: number;
    chat: { id: number; type?: string; username?: string };
    text?: string;
    from?: { language_code?: string; username?: string; first_name?: string };
    reply_to_message?: { from?: { username?: string; is_bot?: boolean } };
    entities?: { type: string; offset: number; length: number }[];
  };
  callback_query?: {
    id: string;
    data?: string;
    message?: { chat: { id: number }; message_id?: number };
    from?: { language_code?: string; id: number; username?: string; first_name?: string };
  };
};

const BOT_AT = `@${BOT_HANDLE}`.toLowerCase();

/** 「七星聚 · 龙行无界」兑换码：/start loong_LOONG-XXXXXX 深链，或私聊直接发码 */
function extractDragonCode(text: string): string | null {
  const m =
    text.match(/^\/start\s+loong_(LOONG-[A-Z0-9]{6})$/i) ??
    text.match(/^(LOONG-[A-Z0-9]{6})$/i);
  return m ? m[1].toUpperCase() : null;
}

async function handleDragonRedeem(
  chatId: number,
  code: string,
  lang: "zh" | "en",
  from?: { username?: string; first_name?: string }
) {
  const zh = lang === "zh";
  const name = from?.username ? `@${from.username}` : from?.first_name ?? "";
  const r = await redeemDragonCode(code, chatId, name);

  if (!r.ok) {
    await sendText(
      chatId,
      r.reason === "expired"
        ? zh
          ? `⌛ 这枚龙珠兑换码已过期（有效期 14 天）。别灰心——星珠还在官网等你，新一轮集齐即可再召唤界龙。`
          : `⌛ This LOONG code has expired (14-day validity). Collect 7 pearls again on the site to summon another wish.`
        : zh
          ? `未找到这枚兑换码，请核对（形如 <code>LOONG-ABC123</code>）。`
          : `Code not found — please double-check (format: <code>LOONG-ABC123</code>).`
    );
    return;
  }

  const kind = r.rec.kind;
  const perfect = r.rec.perfect;
  if (r.already) {
    await sendText(
      chatId,
      zh
        ? `ℹ️ 这枚兑换码已核销过（${r.rec.redeemedAt?.slice(0, 10) ?? ""}）。如有疑问请联系 ${TELEGRAM_DISPLAY}。`
        : `ℹ️ This code was already redeemed (${r.rec.redeemedAt?.slice(0, 10) ?? ""}). Questions? ${TELEGRAM_DISPLAY}.`
    );
    return;
  }

  if (kind === "grand") {
    await sendText(
      chatId,
      zh
        ? `🐲 <b>界龙之约 · 三鳞大成</b>\n\n三枚月令龙鳞已兑现，这是坚持三个月的约定：\n· 任一产品<b>年付 8 折</b>专属协议价（报码 <code>${code}</code>）\n· <b>定制方案绿色通道</b>：需求直达工程师排期\n· 专属 1v1 对接：${TELEGRAM_DISPLAY}\n\n工作人员将在 24 小时内与你确认权益。`
        : `🐲 <b>The Loong's Covenant</b>\n\nThree monthly scales redeemed — three months of dedication:\n· <b>20% off yearly</b> on any product (quote <code>${code}</code>)\n· <b>Fast-track custom solutions</b> straight to engineering\n· Dedicated 1-on-1: ${TELEGRAM_DISPLAY}\n\nOur team will confirm within 24h.`
    );
  } else if (kind === "trial") {
    await sendText(
      chatId,
      zh
        ? `🐉 <b>界龙的赐福 · 愿望成真</b>\n\n你的「愿·体验」已核销：<b>任选一款产品 30 天全功能体验</b>${perfect ? "\n✨ 北斗正位加成：体验期 +7 天！" : ""}\n\n工作人员将在 24 小时内私信为你开通；想加速可直接联系 ${TELEGRAM_DISPLAY} 并出示码 <code>${code}</code>。`
        : `🐉 <b>The Boundless Loong grants your wish</b>\n\nYour trial wish is confirmed: <b>30-day full access to any one product</b>${perfect ? "\n✨ Big Dipper Perfect bonus: +7 extra days!" : ""}\n\nOur team will DM you within 24h — or contact ${TELEGRAM_DISPLAY} with code <code>${code}</code> to fast-track.`
    );
  } else {
    await sendText(
      chatId,
      zh
        ? `🎁 <b>界龙的机缘礼包</b>\n\n· 专属协议价：任一产品首月 <b>9 折</b>（报码 <code>${code}</code>）\n· <b>1v1 方案优先通道</b>：直接联系 ${TELEGRAM_DISPLAY}\n· 彩蛋线索：官网右下角的小家伙，<i>连点 7 次</i>会发生什么呢…${perfect ? "\n✨ 北斗正位加成：折扣升级为 85 折！" : ""}`
        : `🎁 <b>The Loong's Fortune Pack</b>\n\n· Exclusive deal: <b>10% off</b> first month on any product (quote <code>${code}</code>)\n· <b>Priority 1-on-1 consult</b>: ${TELEGRAM_DISPLAY}\n· Easter-egg hint: try clicking the little bot on our site <i>7 times in a row</i>…${perfect ? "\n✨ Perfect-Dipper bonus: discount upgraded to 15% off!" : ""}`
    );
  }

  await notifyAdmins(
    `🐉 龙珠核销 <code>${code}</code>\n类型：${kind === "grand" ? "界龙之约（三鳞大奖）" : kind === "trial" ? "体验月卡" : "机缘礼包"}${perfect ? "（北斗正位）" : ""}\n用户：${name || "-"} (chat_id: <code>${chatId}</code>)\n${kind === "grand" ? "⚠️ 高价值客户：请 24h 内 1v1 对接年付权益" : kind === "trial" ? "⚠️ 请 24h 内开通体验授权" : "折扣码已生效，跟进即可"}`
  );
}

/** 星珠进度一行文案：●●●○○○○（3/7） */
function pearlBar(state: DragonState): string {
  const n = state.collected;
  return `${"●".repeat(n)}${"○".repeat(Math.max(0, 7 - n))}（${n}/7）`;
}

const REMIND_HOUR = Number(process.env.DRAGON_REMIND_HOUR ?? 19);

/** 提醒开关内联键盘（点按走 xz_remind_* callback） */
function remindKeyboard(zh: boolean) {
  return [
    [
      { text: zh ? `🔔 每日 ${REMIND_HOUR}:00 提醒我` : `🔔 Remind me daily ${REMIND_HOUR}:00`, callback_data: "xz_remind_on" },
      { text: zh ? "🔕 关闭提醒" : "🔕 Mute", callback_data: "xz_remind_off" },
    ],
  ];
}

/** bot 端 /xingzhu 每日收珠（已绑定=网页进度同步，未绑定=独立进度、日后绑定合并） */
async function handleXingzhu(chatId: number, lang: "zh" | "en") {
  const zh = lang === "zh";
  const r = await collectPearlByTg(chatId);
  const st = r.state;
  if (r.already) {
    await sendText(
      chatId,
      zh
        ? `今日星珠已收过啦 ${pearlBar(st)}\n明天再来点亮下一颗。`
        : `Today's pearl is already lit ${pearlBar(st)}\nCome back tomorrow for the next one.`,
      remindKeyboard(zh)
    );
    return;
  }
  if (!r.ok) {
    await sendText(
      chatId,
      zh
        ? `七星已聚齐！先回官网召唤界龙、许下愿望，新一轮才会开始：\n${SITE_URL}`
        : `All seven stars are aligned! Summon the Loong on our site first:\n${SITE_URL}`
    );
    return;
  }
  const done = st.collected >= 7;
  const tail = r.bound
    ? ""
    : zh
      ? `\n\n💡 网页端也在集珠？打开官网右下角龙珠面板点「在 Telegram 同步」即可合并进度。`
      : `\n\n💡 Also collecting on the web? Use "Sync on Telegram" in the site's pearl panel to merge progress.`;
  await sendText(
    chatId,
    done
      ? zh
        ? `🐉 <b>七星聚齐！</b> ${pearlBar(st)}\n回官网召唤界龙、领取你的愿望：\n${SITE_URL}${tail}`
        : `🐉 <b>Seven stars aligned!</b> ${pearlBar(st)}\nSummon the Loong on our site to claim your wish:\n${SITE_URL}${tail}`
      : zh
        ? `✨ 第 ${st.collected} 颗星珠归位 ${pearlBar(st)}${st.collected === 6 ? "\n只差一颗，明日界龙将至！" : ""}${tail}`
        : `✨ Pearl #${st.collected} aligned ${pearlBar(st)}${st.collected === 6 ? "\nOne more — the Loong arrives tomorrow!" : ""}${tail}`,
    remindKeyboard(zh)
  );
}

/** Groups we will answer in: the configured public group + optional id allowlist. */
function isAllowedGroup(chat: { username?: string; id: number }): boolean {
  if (chat.username && chat.username.toLowerCase() === TELEGRAM_GROUP.toLowerCase()) return true;
  const ids = (process.env.TELEGRAM_GROUP_IDS || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  return ids.includes(String(chat.id));
}

export async function POST(req: NextRequest) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) return NextResponse.json({ ok: true });

  const secret = process.env.TELEGRAM_WEBHOOK_SECRET;
  if (secret) {
    const hdr = req.headers.get("x-telegram-bot-api-secret-token");
    if (hdr !== secret) {
      return NextResponse.json({ ok: false }, { status: 403 });
    }
  }

  let update: TgUpdate;
  try {
    update = await req.json();
  } catch {
    return NextResponse.json({ ok: true });
  }

  // idempotency: ignore Telegram retries of an already-processed update
  if (isDuplicateUpdate(update.update_id)) {
    return NextResponse.json({ ok: true });
  }

  try {
    if (update.callback_query) {
      const cq = update.callback_query;
      const chatId = cq.message?.chat.id;
      const data = cq.data ?? "";
      if (chatId && data) {
        const lang = detectLang(cq.from?.language_code);
        /* 龙珠提醒开关：xz_remind_on/off（answerCallback 的 text 会以 toast 弹给用户） */
        if (data === "xz_remind_on" || data === "xz_remind_off") {
          const on = data.endsWith("_on");
          await setRemindByTg(cq.from?.id ?? chatId, on);
          const zh = String(lang) === "zh";
          await answerCallback(
            cq.id,
            on
              ? zh ? `🔔 已开启：每天 ${process.env.DRAGON_REMIND_HOUR ?? 19}:00 没收珠就提醒你` : `🔔 Daily reminder on (${process.env.DRAGON_REMIND_HOUR ?? 19}:00)`
              : zh ? "🔕 已关闭提醒" : "🔕 Reminder muted"
          );
          return NextResponse.json({ ok: true });
        }
        const from = cq.from
          ? { id: cq.from.id, username: cq.from.username, first_name: cq.from.first_name }
          : undefined;
        await handleCallback(chatId, data, cq.id, lang, from, cq.message?.message_id);
      }
      return NextResponse.json({ ok: true });
    }

    const msg = update.message;
    if (!msg?.text || !msg.chat?.id) {
      return NextResponse.json({ ok: true });
    }

    const chatId = msg.chat.id;
    const text = msg.text.trim();
    const lang = detectLang(msg.from?.language_code);
    const chatType = msg.chat.type ?? "private";

    // ── group / supergroup: answer only when invoked, never flood ──
    if (chatType === "group" || chatType === "supergroup") {
      if (!isAllowedGroup(msg.chat)) {
        return NextResponse.json({ ok: true });
      }
      const lower = text.toLowerCase();
      const repliedToBot =
        msg.reply_to_message?.from?.is_bot === true &&
        msg.reply_to_message?.from?.username?.toLowerCase() === BOT_HANDLE.toLowerCase();
      const mentioned = lower.includes(BOT_AT);
      const isCmd = text.startsWith("/");
      // command must target our bot (or be untargeted) to avoid hijacking other bots
      const cmdForUs = isCmd && (lower.includes(BOT_AT) || !lower.includes("@"));

      if (!(mentioned || repliedToBot || (isCmd && cmdForUs))) {
        return NextResponse.json({ ok: true });
      }

      // strip @botname mention and any leading /command → clean question
      const question = text
        .replace(new RegExp(BOT_AT, "gi"), "")
        .replace(/^\/[a-z0-9_]+/i, "")
        .trim();

      // NOTE: web_app buttons are invalid in groups; handleGroupMessage uses url-only.
      await handleGroupMessage(chatId, question, lang, msg.message_id);
      return NextResponse.json({ ok: true });
    }

    // ── 龙珠彩蛋核销：/start loong_<code> 深链 或 私聊直接发 LOONG-XXXXXX ──
    const dragonCode = extractDragonCode(text);
    if (dragonCode) {
      await handleDragonRedeem(chatId, dragonCode, lang === "zh" ? "zh" : "en", msg.from);
      return NextResponse.json({ ok: true });
    }

    // ── 龙珠进度绑定：/start xz_<签名令牌>（官网龙珠面板「在 Telegram 同步」深链） ──
    const bindMatch = text.match(/^\/start\s+(xz_[A-Za-z0-9-]{40,64})$/i);
    if (bindMatch) {
      const vid = parseStartToken(bindMatch[1]);
      const zh = lang === "zh";
      if (!vid) {
        await sendText(chatId, zh ? "绑定链接已失效，请回官网重新点「在 Telegram 同步」。" : "This link has expired — tap “Sync on Telegram” on the site again.");
        return NextResponse.json({ ok: true });
      }
      const r = await bindTelegram(vid, chatId, msg.from?.username ? `@${msg.from.username}` : msg.from?.first_name);
      await sendText(
        chatId,
        zh
          ? `🔗 <b>星珠进度已同步</b> ${pearlBar(r.state)}${r.merged ? "\n（已合并你在 bot 里的旧进度）" : ""}\n\n以后每天在这里发 /xingzhu 也能收珠，与官网同一份进度。`
          : `🔗 <b>Pearl progress synced</b> ${pearlBar(r.state)}${r.merged ? "\n(merged your previous bot progress)" : ""}\n\nSend /xingzhu here daily to collect — same progress as the website.`
      );
      return NextResponse.json({ ok: true });
    }

    // ── 龙珠每日签到：/xingzhu 或 频道按钮深链 /start xingzhu ──
    if (/^\/xingzhu\b/i.test(text) || /^\/start\s+xingzhu$/i.test(text)) {
      await handleXingzhu(chatId, lang === "zh" ? "zh" : "en");
      return NextResponse.json({ ok: true });
    }

    // admin self-binding for lead notifications
    if (text.startsWith("/bindadmin")) {
      const arg = text.split(/\s+/)[1] ?? "";
      const key = process.env.TELEGRAM_SETUP_KEY;
      if (key && arg === key) {
        const added = await bindAdminChat(chatId);
        await sendText(
          chatId,
          added
            ? `✅ 已绑定为留资接收人\nchat_id: <code>${chatId}</code>\n以后有人留资会推送到这里。`
            : `ℹ️ 你已经是留资接收人了（chat_id: <code>${chatId}</code>）。`
        );
      } else {
        await sendText(chatId, "❌ 口令错误。用法：/bindadmin <setup_key>");
      }
      return NextResponse.json({ ok: true });
    }
    if (text.startsWith("/unbindadmin")) {
      await unbindAdminChat(chatId);
      await sendText(chatId, "✅ 已取消留资推送（本会话）。");
      return NextResponse.json({ ok: true });
    }

    // ── 管理员专属命令（仅已绑定的 admin 会话可用；他人静默走普通问答，不暴露命令存在）──
    if (/^\/(ops|diag)\b/i.test(text) && (await getAdminChats()).includes(String(chatId))) {
      console.log(`[tg-admin] chat=${chatId} cmd=${text.slice(0, 40)}`); // 审计：谁在拉运营数据/取诊断包
      if (/^\/ops\b/i.test(text)) {
        const { buildOpsSummary, formatOpsSummary } = await import("@/lib/ops-summary");
        const s = await buildOpsSummary().catch(() => null);
        await sendText(chatId, s ? formatOpsSummary(s) : "⚠ 汇总失败，稍后再试或看服务器日志。");
        return NextResponse.json({ ok: true });
      }
      const code = (text.split(/\s+/)[1] ?? "").trim().toUpperCase();
      if (!/^[A-Z0-9]{6}$/.test(code)) {
        await sendText(chatId, "用法：<code>/diag 六位码</code>（客户上传诊断包后报给你的短码）");
        return NextResponse.json({ ok: true });
      }
      const { readFile } = await import("fs/promises");
      const path = await import("path");
      const { DATA_DIR } = await import("@/lib/data-dir");
      try {
        const zip = await readFile(path.join(DATA_DIR, "diag", `${code}.zip`));
        const meta = await readFile(path.join(DATA_DIR, "diag", `${code}.json`), "utf-8")
          .then((s) => JSON.parse(s))
          .catch(() => ({}));
        const cap = `🧰 诊断包 ${code}` + (meta.app ? ` · v${meta.app}` : "") + (meta.t ? ` · ${String(meta.t).slice(0, 16)}` : "");
        const r = await sendDocument(chatId, `diag-${code}.zip`, zip, cap);
        if (!r?.ok) await sendText(chatId, "⚠ 文件发送失败（可能超时），可从服务器 diag 目录取。");
      } catch {
        await sendText(chatId, `未找到 <code>${code}</code>——可能码有误，或已过 30 天自动清理。`);
      }
      return NextResponse.json({ ok: true });
    }

    if (text.startsWith("/")) {
      const [cmd, ...rest] = text.split(/\s+/);
      const startArg = cmd.startsWith("/start") && rest[0] ? rest[0] : undefined;
      // /start <订单号>：客户从 /order 结算页深链进来 → 绑定本会话接收到账/开通/临期通知
      if (startArg && /^AH-\d{8}-[A-Z0-9]{4,10}$/i.test(startArg)) {
        const o = await bindOrderNotify(startArg, chatId);
        await sendText(
          chatId,
          o
            ? (lang === "zh"
                ? `🔔 已绑定订单 <code>${o.id}</code>\n到账、开通、临期都会第一时间通知你。当前状态：${
                    { pending: "待付款", paid: "已到账·开通中", activated: "已开通", cancelled: "已取消" }[o.status] ?? o.status
                  }。`
                : `🔔 Bound to order <code>${o.id}</code>\nYou'll get payment, activation and renewal alerts here.`)
            : (lang === "zh"
                ? "未找到该订单号，请核对下单时获得的单号（形如 AH-20260711-XXXXXX）。"
                : "Order not found — check the ID from checkout (AH-20260711-XXXXXX).")
        );
        return NextResponse.json({ ok: true });
      }
      await handleCommand(chatId, cmd, lang, startArg);
    } else {
      await handleFreeText(chatId, text, lang);
    }
  } catch {
    /* never fail webhook — TG will retry */
  }

  return NextResponse.json({ ok: true });
}
