// /api/console/telegram —— Telegram 运营配置：bot（名称 / token）、群与频道、客服工作时间、邀请链接、工单概览。
// GET：脱敏配置 + 工单统计（viewer+）。POST { action, ... }：写操作 admin+，全部写 audit（entity="tg_hub"，不含 token）。
import { NextRequest, NextResponse } from "next/server";
import { getConsoleUser } from "@/lib/console-auth";
import { roleAtLeast } from "@/lib/console-users";
import { writeAudit } from "@/lib/ledger";
import { listTickets, ticketStats } from "@/lib/chatx-tickets";
import { loadHub } from "@/lib/tg-hub-store";
import { PostError, cancelPost, listPosts, schedulePost } from "@/lib/tg-posts";
import { KnownIssueError, listKnownIssueRows, removeKnownIssue, saveKnownIssue } from "@/lib/chatx-known-issues";
import { HubError, addBot, applyWebhook, createInvite, publicHub, setSupport, updateBot, updateChat, verifyChat, webhookInfo } from "@/lib/tg-hub-admin";
import type { HubChatRole } from "@/lib/tg-hub-store";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  if (!getConsoleUser(req)) return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  const hub = publicHub(await loadHub());
  const tickets = await listTickets();
  return NextResponse.json({
    ok: true,
    ...hub,
    knownIssues: await listKnownIssueRows(),
    posts: (await listPosts()).slice(-30).reverse(),
    tickets: { d7: ticketStats(tickets, Date.now() - 7 * 86400_000), recent: tickets.slice(-30).reverse() },
  });
}

const READ_ACTIONS = new Set(["webhook_info"]);
const str = (v: unknown) => (typeof v === "string" ? v : v === undefined || v === null ? "" : String(v));

export async function POST(req: NextRequest) {
  const user = getConsoleUser(req);
  if (!user) return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  let body: Record<string, unknown> = {};
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid json body" }, { status: 400 });
  }
  const action = str(body.action);
  if (!READ_ACTIONS.has(action) && !roleAtLeast(user.role, "admin")) {
    return NextResponse.json({ error: "forbidden: admin role required" }, { status: 403 });
  }
  const audit = (entityId: string, detail: unknown) =>
    writeAudit({ actor: `console:${user.username}`, action: `tg_hub.${action}`, entity: "tg_hub", entity_id: entityId, detail });
  try {
    switch (action) {
      case "webhook_info":
        return NextResponse.json({ ok: true, info: await webhookInfo(str(body.botId)) });
      case "add_bot": {
        const bot = await addBot({ name: str(body.name), token: str(body.token) });
        audit(bot.id, { name: bot.name, username: bot.username });
        return NextResponse.json({ ok: true, bot });
      }
      case "update_bot": {
        const patch = {
          name: body.name === undefined ? undefined : str(body.name),
          enabled: typeof body.enabled === "boolean" ? body.enabled : undefined,
          token: body.token ? str(body.token) : undefined,
        };
        const r = await updateBot(str(body.id), patch);
        audit(r.id, { name: patch.name, enabled: patch.enabled, tokenChanged: r.tokenChanged });
        return NextResponse.json({ ok: true, ...r });
      }
      case "apply_webhook": {
        const r = await applyWebhook(str(body.botId));
        audit(str(body.botId), { ok: r.ok, allowedUpdates: r.allowedUpdates, steps: r.steps });
        return NextResponse.json(r);
      }
      case "verify_chat": {
        const role = body.role ? (str(body.role) as HubChatRole) : undefined;
        const enable = typeof body.enable === "boolean" ? body.enable : undefined;
        const r = await verifyChat(str(body.botId), str(body.chat), { role, enable });
        audit(`${r.chat.botId}:${r.chat.chatId}`, { title: r.chat.title, role: r.chat.role, enabled: r.chat.enabled, botStatus: r.chat.botStatus, missing: r.missing });
        return NextResponse.json({ ok: true, ...r });
      }
      case "update_chat": {
        const patch = {
          role: body.role === undefined ? undefined : str(body.role),
          enabled: typeof body.enabled === "boolean" ? body.enabled : undefined,
          features: typeof body.features === "object" && body.features ? (body.features as Record<string, boolean>) : undefined,
          title: body.title === undefined ? undefined : str(body.title),
          langs: body.langs === undefined ? undefined : str(body.langs),
        };
        const chat = await updateChat(str(body.botId), str(body.chatId), patch);
        audit(`${chat.botId}:${chat.chatId}`, patch);
        return NextResponse.json({ ok: true, chat });
      }
      case "create_invite": {
        const inv = await createInvite(str(body.botId), str(body.chatId), str(body.src));
        audit(`${inv.botId}:${inv.chatId}`, { src: inv.src });
        return NextResponse.json({ ok: true, invite: inv });
      }
      case "set_support": {
        const s = await setSupport({ hours: body.hours === undefined ? undefined : str(body.hours), tzOffset: body.tzOffset === undefined ? undefined : Number(body.tzOffset), slaMin: body.slaMin === undefined ? undefined : Number(body.slaMin), escalateMin: body.escalateMin === undefined ? undefined : Number(body.escalateMin), duty: body.duty === undefined ? undefined : str(body.duty), followupMin: body.followupMin === undefined ? undefined : Number(body.followupMin) });
        audit("support", s);
        return NextResponse.json({ ok: true, support: s });
      }
      case "known_issue_save": {
        const rec = await saveKnownIssue({ id: str(body.id), keywords: str(body.keywords), zh: str(body.zh), en: str(body.en), enabled: body.enabled !== false });
        audit(rec.id, rec);
        return NextResponse.json({ ok: true, issue: rec });
      }
      case "post_schedule": {
        const post = await schedulePost({ botId: str(body.botId), chatId: str(body.chatId), text: str(body.text), sendAt: str(body.sendAt) || undefined, buttonText: str(body.buttonText), buttonUrl: str(body.buttonUrl), photo: str(body.photo), repeat: str(body.repeat), createdBy: user.username });
        audit(`${post.botId}:${post.chatId}`, { postId: post.id, sendAt: post.sendAt, chars: post.text.length, button: post.button?.url, photo: post.photo, repeat: post.repeat });
        return NextResponse.json({ ok: true, post });
      }
      case "post_cancel": {
        const post = await cancelPost(Number(body.id));
        audit(`${post.botId}:${post.chatId}`, { postId: post.id, canceled: true });
        return NextResponse.json({ ok: true, post });
      }
      case "known_issue_delete": {
        const removed = await removeKnownIssue(str(body.id));
        audit(str(body.id), { removed });
        return NextResponse.json({ ok: true, removed });
      }
      default:
        return NextResponse.json({ error: `unknown action: ${action}` }, { status: 400 });
    }
  } catch (e) {
    if (e instanceof HubError || e instanceof KnownIssueError || e instanceof PostError) return NextResponse.json({ error: e.message }, { status: 400 });
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
