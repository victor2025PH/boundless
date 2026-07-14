import { NextRequest, NextResponse } from "next/server";
import { detectLang } from "@/lib/bot-knowledge";
import {
  handleCallback,
  handleCommand,
  handleFreeText,
  handleGroupMessage,
  sendDocument,
  sendText,
} from "@/lib/telegram-bot";
import { bindAdminChat, getAdminChats, unbindAdminChat } from "@/lib/admin-store";
import { bindOrderNotify } from "@/lib/order-store";
import { isDuplicateUpdate } from "@/lib/tg-dedup";
import { BOT_HANDLE, TELEGRAM_GROUP } from "@/lib/site";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type TgUpdate = {
  update_id?: number;
  message?: {
    message_id?: number;
    chat: { id: number; type?: string; username?: string };
    text?: string;
    from?: { language_code?: string };
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
