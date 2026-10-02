/**
 * ChatX bot 客服与报障：
 *   · 转人工 → 在控制台登记的客服群里开工单话题，用户 ⇄ 客服经 bot 双向转发；没配客服群时回落原「直达链 + 通知管理员」。
 *   · 报障 → 选问题类型 → 发机器码 / 诊断回执号 / 截图；机器码查客户端错误回传给出版本与自助建议，回执号直接挂到工单。
 * 机器码、回执号、错误详情只出现在私聊和私有客服群里。
 */
import crypto from "crypto";
import { access } from "fs/promises";
import path from "path";
import type { BotLang } from "./bot-knowledge";
import { normalizeFingerprint } from "./ai-gateway";
import { readClientLogRows } from "./client-logs";
import { CHATX } from "./chatxContent";
import { DATA_DIR } from "./data-dir";
import { hasPendingDiag, loadDiagRequests, saveDiagRequests } from "./diag-requests";
import { KNOWN_ISSUES, knownIssueText, matchKnownIssue, refreshKnownIssues } from "./chatx-known-issues";
import {
  answerCallback,
  downloadTgFile,
  escHtml,
  handleHuman,
  lastSrc,
  recallHistory,
  sendText,
  tgCall,
  tgUpload,
  type InlineBtn,
  type TgFrom,
} from "./chatx-bot";
import { BUILTIN_BOT_ID, currentBot, withBot, type BotCtx } from "./tg-bot-context";
import { botCtxById, chatKey, dutyNow, inSupportHours, loadHub, pickSupportChat } from "./tg-hub-store";
import { activeTicketFor, createTicket, getTicket, isOverdue, listTickets, ticketForSupportMessage, updateTicket, type Ticket } from "./chatx-tickets";
import { trackTg } from "./tg-events";
import { markFollowupDone, recordKnownShown, takeDueFollowups } from "./chatx-bug-followup";
import { notifyAdmins } from "./order-store";
import { SITE_URL } from "./site";

const DIAG_DIR = path.join(DATA_DIR, "diag");
const RELAY_MAX_BYTES = 20 * 1024 * 1024;
const BUG_STATE_TTL_MS = 30 * 60_000;

export const FP_RE = /\b([0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4})\b/i;
const CODE_RE = /\b([23456789ABCDEFGHJKMNPQRSTUVWXYZ]{6})\b/g;

export const BUG_KINDS = ["install", "login", "feature", "crash", "other"] as const;
export type BugKind = (typeof BUG_KINDS)[number];

const KIND_LABEL: Record<BugKind, [string, string]> = {
  install: ["装不上 / 打不开", "Can't install / open"],
  login: ["登录 / 授权", "Login / license"],
  feature: ["功能不正常", "Feature not working"],
  crash: ["闪退 / 卡死", "Crash / freeze"],
  other: ["其他问题", "Something else"],
};

type BugState = { kind: BugKind; at: number; fp?: string; diag?: string; note?: string; awaitingNote: boolean; /** 最近给用户看过的已知问题 id（统计「解决了 / 转人工」用） */ known?: string };
const bugStates = new Map<string, BugState>();

const stateKey = (uid: number) => `${currentBot()?.id ?? BUILTIN_BOT_ID}:${uid}`;

function getBugState(uid: number): BugState | undefined {
  const s = bugStates.get(stateKey(uid));
  if (!s) return undefined;
  if (Date.now() - s.at > BUG_STATE_TTL_MS) {
    bugStates.delete(stateKey(uid));
    return undefined;
  }
  return s;
}

function setBugState(uid: number, patch: Partial<BugState>) {
  const prev = getBugState(uid) ?? { kind: "other" as BugKind, at: Date.now(), awaitingNote: false };
  bugStates.set(stateKey(uid), { ...prev, ...patch, at: Date.now() });
}

export function kindLabel(kind: string, lang: BotLang): string {
  const k = (BUG_KINDS as readonly string[]).includes(kind) ? (kind as BugKind) : "other";
  return KIND_LABEL[k][lang === "zh" ? 0 : 1];
}

const whoOf = (from: TgFrom) => from.first_name || from.username || String(from.id);

// ── 机器码 / 回执号识别 ─────────────────────────────────────────────

export function extractFingerprint(text: string): string | null {
  const m = text.match(FP_RE);
  return m ? normalizeFingerprint(m[1]) : null;
}

/** 文本里的 6 位诊断回执号：必须真有这份诊断包才算（防把普通单词当回执号）。 */
export async function extractDiagCode(text: string): Promise<string | null> {
  for (const m of text.toUpperCase().matchAll(CODE_RE)) {
    const code = m[1];
    if (!/\d/.test(code)) continue;
    try {
      await access(path.join(DIAG_DIR, `${code}.json`));
      return code;
    } catch {
      /* not a receipt */
    }
  }
  return null;
}

export type FpSummary = { fp: string; seen: boolean; version?: string; lastSeen?: string; errors: number; crashes: number; topError?: string; outdated: boolean };

export async function summarizeFingerprint(fp: string, windowHours = 72): Promise<FpSummary> {
  const rows = ((await readClientLogRows(windowHours)) ?? []).filter((r) => normalizeFingerprint(r.fp) === fp);
  if (!rows.length) return { fp, seen: false, errors: 0, crashes: 0, outdated: false };
  const latest = rows.reduce((a, b) => (a.t >= b.t ? a : b));
  const counts = new Map<string, number>();
  let errors = 0;
  let crashes = 0;
  for (const r of rows) {
    if (/exit_sentinel|crash/i.test(r.logger)) crashes += 1;
    if (/error|critical|fatal/i.test(r.level)) {
      errors += r.n;
      const k = r.msg.slice(0, 160);
      counts.set(k, (counts.get(k) ?? 0) + r.n);
    }
  }
  const topError = [...counts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0];
  const version = latest.ver || undefined;
  return { fp, seen: true, version, lastSeen: latest.t, errors, crashes, topError, outdated: Boolean(version && cmpVer(version, CHATX.download.version) < 0) };
}

function cmpVer(a: string, b: string): number {
  const pa = a.replace(/^v/i, "").split(".").map((x) => parseInt(x, 10) || 0);
  const pb = b.replace(/^v/i, "").split(".").map((x) => parseInt(x, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] ?? 0) - (pb[i] ?? 0);
    if (d) return d;
  }
  return 0;
}

// ── 报障流程 ───────────────────────────────────────────────────────

function kindKeyboard(lang: BotLang): InlineBtn[][] {
  const zh = lang === "zh";
  const b = (k: BugKind) => ({ text: KIND_LABEL[k][zh ? 0 : 1], callback_data: `cxb:k:${k}` });
  return [[b("install"), b("login")], [b("feature"), b("crash")], [b("other"), { text: zh ? "👤 直接找人工" : "👤 Talk to a human", callback_data: "cxb:h" }]];
}

function resultKeyboard(lang: BotLang, withDiag: boolean): InlineBtn[][] {
  const zh = lang === "zh";
  const rows: InlineBtn[][] = [
    [
      { text: zh ? "✅ 解决了" : "✅ Solved", callback_data: "cxb:ok" },
      { text: zh ? "👤 还不行，转人工" : "👤 Still stuck — human", callback_data: "cxb:h" },
    ],
  ];
  if (withDiag) rows.push([{ text: zh ? "📤 允许客服远程取日志" : "📤 Let support fetch my logs", callback_data: "cxb:diag" }]);
  return rows;
}

function askCodeText(kind: BugKind, lang: BotLang): string {
  const zh = lang === "zh";
  const lines = zh
    ? [
        `🩺 <b>${KIND_LABEL[kind][0]}</b> —— 发下面任意一样给我，客服能直接定位：`,
        "1️⃣ <b>机器码</b>：ChatX 客服 / 支持页里的「机器码」，点「复制」后粘贴过来（形如 <code>ABCD-1234-EF56-7890</code>）",
        "2️⃣ <b>诊断回执号</b>：同一页点「🩺 一键发给客服」，把显示的 6 位回执号发来（只含运行日志与配置，不含聊天内容和账号密码）",
        "3️⃣ <b>截图</b>或一句话描述问题",
        "",
        "🔒 机器码只用于定位你的设备日志，请只在这里私聊发送，不要发到群里。",
      ]
    : [
        `🩺 <b>${KIND_LABEL[kind][1]}</b> — send me any of these so support can pinpoint it:`,
        "1️⃣ <b>Machine code</b>: on ChatX's support page, tap “Copy” next to the machine code and paste it here (like <code>ABCD-1234-EF56-7890</code>)",
        "2️⃣ <b>Diagnostic receipt</b>: on the same page tap “🩺 Send to support” and send me the 6-character receipt (logs and settings only — no chats or passwords)",
        "3️⃣ A <b>screenshot</b> or a one-line description",
        "",
        "🔒 Your machine code is only used to find your device's logs — send it here in private, never in a group.",
      ];
  if (kind === "install") {
    lines.push(
      "",
      zh ? "💡 还没装上、没有机器码的话，先试试：" : "💡 Not installed yet (no machine code)? Try these first:",
      "• " + knownIssueText(matchKnownIssue("smartscreen", KNOWN_ISSUES)!, lang),
      "• " + knownIssueText(matchKnownIssue("杀毒", KNOWN_ISSUES)!, lang)
    );
  }
  return lines.join("\n");
}

export async function startBug(chatId: number, from: TgFrom, lang: BotLang) {
  const src = await lastSrc(from.id);
  setBugState(from.id, { kind: "other", awaitingNote: false, fp: undefined, diag: undefined, note: undefined });
  await sendText(
    chatId,
    lang === "zh" ? "🩺 <b>报障 / 求助</b>\n遇到了什么问题？选一个最接近的：" : "🩺 <b>Report a problem</b>\nWhat's going on? Pick the closest one:",
    kindKeyboard(lang)
  );
  await trackTg("chatx_bot_bug", { step: "start", src, uid: from.id });
}

function fpSummaryText(s: FpSummary, lang: BotLang): string {
  const zh = lang === "zh";
  if (!s.seen) {
    return zh
      ? `🔎 收到机器码 <code>${s.fp}</code>。最近 72 小时这台设备没有上报过运行记录（可能没打开过 ChatX，或网络连不上服务器）。\n描述一下问题，或点「转人工」让客服接手。`
      : `🔎 Got machine code <code>${s.fp}</code>. This device hasn't reported anything in the last 72 hours (ChatX may not have been opened, or it can't reach our server).\nDescribe the problem, or tap "human" to hand it to support.`;
  }
  const lines = [
    zh ? `🔎 找到你的设备 <code>${s.fp}</code>` : `🔎 Found your device <code>${s.fp}</code>`,
    zh
      ? `• 版本：${escHtml(s.version ?? "未知")}${s.outdated ? `（最新 ${CHATX.download.version}）` : ""}`
      : `• Version: ${escHtml(s.version ?? "unknown")}${s.outdated ? ` (latest ${CHATX.download.version})` : ""}`,
    zh
      ? `• 最近 72 小时：${s.errors} 条错误${s.crashes ? `，${s.crashes} 次异常退出` : ""}`
      : `• Last 72h: ${s.errors} errors${s.crashes ? `, ${s.crashes} abnormal exits` : ""}`,
  ];
  if (s.outdated) {
    lines.push(
      zh
        ? "\n💡 你的版本不是最新的，很多问题升级后就好了：点菜单里的下载按钮装最新版。"
        : "\n💡 You're not on the latest version — many issues are fixed by updating. Use the download button to install the latest."
    );
  }
  const k = s.topError ? matchKnownIssue(s.topError) : null;
  if (k) lines.push("\n💡 " + knownIssueText(k, lang));
  return lines.join("\n");
}

/**
 * 私聊文字里的报障线索：机器码 / 回执号，或报障流程中用户的问题描述。
 * 返回 true 表示已处理（不再走 AI）。
 */
export async function handleSupportText(chatId: number, from: TgFrom, text: string, lang: BotLang): Promise<boolean> {
  const zh = lang === "zh";
  await refreshKnownIssues();
  const src = await lastSrc(from.id);
  const diag = await extractDiagCode(text);
  const fp = extractFingerprint(text);
  if (diag) {
    setBugState(from.id, { diag, fp: fp ?? getBugState(from.id)?.fp, awaitingNote: false });
    await trackTg("chatx_bot_bug", { step: "diag", src, uid: from.id });
    await sendText(
      chatId,
      zh
        ? `📦 收到诊断回执号 <code>${diag}</code>，诊断包已在客服这边，正在为你转人工……`
        : `📦 Got diagnostic receipt <code>${diag}</code> — support already has the package. Connecting you to a human…`
    );
    await supportHuman(chatId, from, lang, { kind: getBugState(from.id)?.kind ?? "other" });
    return true;
  }
  if (fp) {
    const s = await summarizeFingerprint(fp);
    const fk = s.topError ? matchKnownIssue(s.topError)?.id : undefined;
    setBugState(from.id, { fp, awaitingNote: !getBugState(from.id)?.note, ...(fk ? { known: fk } : {}) });
    await trackTg("chatx_bot_bug", { step: "fp", src, uid: from.id, seen: s.seen, outdated: s.outdated, known: fk });
    if (fk) await recordKnownShown({ botId: currentBot()?.id ?? BUILTIN_BOT_ID, uid: from.id, chatId, lang, known: fk, src });
    const active = await activeTicketFor(currentBot()?.id ?? BUILTIN_BOT_ID, from.id);
    if (active) {
      await updateTicket(active.id, (t) => {
        t.fp = fp;
      });
      await postToTicket(active, `🔎 用户补充机器码 <code>${fp}</code>\n${escHtml(fpAgentLine(s))}`);
    }
    await sendText(chatId, fpSummaryText(s, lang), resultKeyboard(lang, s.seen));
    return true;
  }
  const st = getBugState(from.id);
  if (st?.awaitingNote && !text.startsWith("/")) {
    const k = matchKnownIssue(text);
    setBugState(from.id, { note: text.slice(0, 500), awaitingNote: false, ...(k ? { known: k.id } : {}) });
    await trackTg("chatx_bot_bug", { step: "note", src, uid: from.id, known: k?.id });
    if (k) await recordKnownShown({ botId: currentBot()?.id ?? BUILTIN_BOT_ID, uid: from.id, chatId, lang, known: k.id, src });
    const body = k
      ? (zh ? "💡 试试这个：\n" : "💡 Try this:\n") + knownIssueText(k, lang)
      : zh
        ? "📝 记下了。点「转人工」让客服接手（你的描述会一起转过去）；有机器码或回执号也可以继续发来。"
        : "📝 Noted. Tap \"human\" to hand it to support (your description goes along); you can still send your machine code or receipt.";
    await sendText(chatId, body, resultKeyboard(lang, false));
    return true;
  }
  return false;
}

async function requestRemoteDiag(chatId: number, from: TgFrom, lang: BotLang) {
  const zh = lang === "zh";
  const fp = getBugState(from.id)?.fp;
  if (!fp) {
    await sendText(chatId, zh ? "先把机器码发给我，我才能帮你请求日志。" : "Send me your machine code first so I can request the logs.");
    return;
  }
  if (!(await hasPendingDiag(fp))) {
    const reqs = await loadDiagRequests();
    reqs.push({ request_id: crypto.randomBytes(6).toString("hex"), fp, note: `tg:${from.id}`, t: new Date().toISOString(), status: "pending" });
    await saveDiagRequests(reqs);
  }
  await trackTg("chatx_bot_bug", { step: "diag_request", uid: from.id, src: await lastSrc(from.id) });
  await sendText(
    chatId,
    zh
      ? "📤 已发起远程取日志：保持 ChatX 打开并联网，通常 1 分钟内自动上传（只含运行日志与配置，不含聊天内容和账号密码）。上传后客服会收到回执号。"
      : "📤 Log request sent: keep ChatX open and online — it uploads automatically, usually within a minute (logs and settings only, no chats or passwords). Support gets the receipt once it's in."
  );
}

/** 报障与工单相关的回调：cxb:*（用户侧报障）、tk:*（工单）。返回 true 表示已处理。 */
export async function handleSupportCallback(
  cq: { id: string; data: string; from: TgFrom; chatId: number; chatType?: string },
  lang: BotLang
): Promise<boolean> {
  const { data, from, chatId } = cq;
  if (data.startsWith("cxb:")) {
    await answerCallback(cq.id);
    const src = await lastSrc(from.id);
    const [, act, arg] = data.split(":");
    if (act === "start") {
      await startBug(chatId, from, lang);
    } else if (act === "k") {
      const kind = ((BUG_KINDS as readonly string[]).includes(arg) ? arg : "other") as BugKind;
      setBugState(from.id, { kind, awaitingNote: true });
      await trackTg("chatx_bot_bug", { step: "kind", kind, src, uid: from.id });
      await sendText(chatId, askCodeText(kind, lang));
    } else if (act === "ok") {
      const fromFollowup = await markFollowupDone(currentBot()?.id ?? BUILTIN_BOT_ID, from.id);
      const known = getBugState(from.id)?.known ?? fromFollowup;
      bugStates.delete(stateKey(from.id));
      await trackTg("chatx_bot_bug", { step: "solved", src, uid: from.id, known });
      await sendText(chatId, lang === "zh" ? "🎉 太好了！还有问题随时发 /bug。" : "🎉 Great! Send /bug any time you need help.");
    } else if (act === "h") {
      const fromFollowup = await markFollowupDone(currentBot()?.id ?? BUILTIN_BOT_ID, from.id);
      await trackTg("chatx_bot_bug", { step: "escalate", src, uid: from.id, known: getBugState(from.id)?.known ?? fromFollowup });
      await supportHuman(chatId, from, lang, { kind: getBugState(from.id)?.kind ?? "other" });
    } else if (act === "diag") {
      await requestRemoteDiag(chatId, from, lang);
    }
    return true;
  }
  if (data.startsWith("tk:")) {
    const [, act, idRaw] = data.split(":");
    const t = await getTicket(Number(idRaw));
    if (!t) {
      await answerCallback(cq.id, lang === "zh" ? "工单不存在" : "Ticket not found");
      return true;
    }
    await answerCallback(cq.id);
    const agentSide = String(chatId) === t.supportChatId && cq.chatType !== "private";
    if (agentSide && (act === "c" || act === "r" || act === "a")) await agentAction(t, act, from);
    else if (!agentSide && from.id === t.uid && (act === "u" || act === "y" || act === "n")) await userAction(t, act, lang);
    return true;
  }
  return false;
}

// ── 工单 ────────────────────────────────────────────────────────────

function fpAgentLine(s: FpSummary): string {
  if (!s.seen) return "72h 内无回传记录";
  const head = `版本 ${s.version ?? "?"}${s.outdated ? "（非最新）" : ""} · 72h 错误 ${s.errors} · 异常退出 ${s.crashes}`;
  return s.topError ? `${head}\n最多的错误：${s.topError}` : head;
}

function agentKeyboard(id: number): InlineBtn[][] {
  return [
    [
      { text: "✋ 我来接", callback_data: `tk:c:${id}` },
      { text: "✅ 已解决", callback_data: `tk:r:${id}` },
      { text: "🤖 交回 AI", callback_data: `tk:a:${id}` },
    ],
  ];
}

function endKeyboard(id: number, lang: BotLang): InlineBtn[][] {
  return [[{ text: lang === "zh" ? "🤖 结束人工，回到 AI" : "🤖 End chat, back to AI", callback_data: `tk:u:${id}` }]];
}

const msgIdOf = (r: { result?: unknown } | null | undefined) => (r?.result as { message_id?: number } | undefined)?.message_id;

async function postToTicket(t: Ticket, html: string, keyboard?: InlineBtn[][]) {
  const bot = await botCtxById(t.supportBotId);
  if (!bot) return null;
  const res = await withBot(bot, () =>
    tgCall("sendMessage", {
      chat_id: t.supportChatId,
      message_thread_id: t.topicId,
      text: html,
      parse_mode: "HTML",
      disable_web_page_preview: true,
      reply_markup: keyboard ? { inline_keyboard: keyboard } : undefined,
    })
  );
  const mid = msgIdOf(res);
  if (mid) await updateTicket(t.id, (x) => void x.msgIds.push(mid));
  return res;
}

/**
 * 客服 SLA 巡检（挂在 /api/admin/order-sla 的 10 分钟 cron 上，也可单独调 /api/admin/support-sla）：
 * 工作时间内，待接且超过 slaMin 分钟没有首次回复的工单 → 在该工单话题里提醒一次（slaAlertAt 去重）。
 */
export async function runSupportSla(now = Date.now()): Promise<{ checked: number; alerted: number; escalated: number; offHours: boolean }> {
  const hub = await loadHub();
  const slaMin = hub.support.slaMin;
  const onDuty = dutyNow(hub.support, now);
  const dutyLine = onDuty.length ? "\n值班：" + onDuty.map((u) => "@" + escHtml(u)).join(" ") : "";
  const due = (await listTickets()).filter((t) => !t.slaAlertAt && isOverdue(t, slaMin, now));
  if (!inSupportHours(hub.support, now)) return { checked: due.length, alerted: 0, escalated: 0, offHours: true };
  let alerted = 0;
  for (const t of due) {
    const waited = Math.round((now - Date.parse(t.createdAt)) / 60000);
    const claimed = await updateTicket(t.id, (x) => {
      if (!x.slaAlertAt) x.slaAlertAt = new Date(now).toISOString();
    });
    if (!claimed || claimed.slaAlertAt !== new Date(now).toISOString()) continue;
    const who = `${escHtml(t.name)}${t.username ? ` @${escHtml(t.username)}` : ""}`;
    const r = await postToTicket(t, `⏰ <b>工单 #${t.id} 已等待 ${waited} 分钟</b>（超过 ${slaMin} 分钟未回复）
${who} · ${t.kind === "human" ? "转人工" : `报障 · ${escHtml(t.kind)}`}
请尽快回复或点「认领」。${dutyLine}`);
    if (r) alerted++;
  }
  let escalated = 0;
  const escMin = hub.support.escalateMin;
  if (escMin > 0) {
    const nowIso = new Date(now).toISOString();
    const late = (await listTickets()).filter((t) => t.slaAlertAt && !t.slaEscalatedAt && isOverdue(t, escMin, now));
    for (const t of late) {
      const marked = await updateTicket(t.id, (x) => {
        if (!x.slaEscalatedAt) x.slaEscalatedAt = nowIso;
      });
      if (!marked || marked.slaEscalatedAt !== nowIso) continue;
      const waited = Math.round((now - Date.parse(t.createdAt)) / 60000);
      const group = hub.chats.find((c) => c.botId === t.supportBotId && c.chatId === t.supportChatId)?.title ?? t.supportChatId;
      await notifyAdmins(
        `🚨 <b>客服工单 #${t.id} 已等待 ${waited} 分钟仍没人回复</b>` +
          `\n用户：${escHtml(t.name)}${t.username ? ` @${escHtml(t.username)}` : ""} · 来源 ${escHtml(t.src)}` +
          `\n客服群：${escHtml(group)} · ${t.kind === "human" ? "转人工" : `报障 · ${escHtml(t.kind)}`}` + dutyLine,
        [[{ text: "打开工单", url: `${SITE_URL}/console/telegram?q=%23${t.id}` }]]
      );
      escalated++;
    }
  }
  return { checked: due.length, alerted, escalated, offHours: false };
}

/** 已知问题追问（同一 10 分钟 cron）：给出方案 30 分钟后用户两个按钮都没点 → 追问一次「解决了吗」。 */
export async function runKnownFollowups(now = Date.now()): Promise<{ asked: number }> {
  let asked = 0;
  const afterMin = (await loadHub()).support.followupMin;
  if (!afterMin) return { asked };
  for (const f of await takeDueFollowups(now, afterMin * 60_000)) {
    const bot = await botCtxById(f.botId);
    if (!bot) continue;
    const lang: BotLang = f.lang === "zh" ? "zh" : "en";
    const r = await withBot(bot, () =>
      sendText(f.chatId, lang === "zh" ? "👋 刚才给你的解决步骤试了吗？问题解决了吗？" : "👋 Did the steps I sent help? Is it fixed now?", resultKeyboard(lang, false))
    );
    await trackTg("chatx_bot_bug", { step: "followup", src: f.src, uid: f.uid, known: f.known });
    if (r?.ok) asked++;
  }
  return { asked };
}

async function notifyUser(t: Ticket, html: string, keyboard?: InlineBtn[][]) {
  const bot = await botCtxById(t.botId);
  if (bot) await withBot(bot, () => sendText(t.chatId, html, keyboard));
}

async function closeTopic(t: Ticket, reopen = false) {
  const bot = await botCtxById(t.supportBotId);
  if (bot && t.topicId) await withBot(bot, () => tgCall(reopen ? "reopenForumTopic" : "closeForumTopic", { chat_id: t.supportChatId, message_thread_id: t.topicId }));
}

/** 转人工：有可用客服群 → 开工单；否则回落原直达链 + 管理员通知。 */
export async function supportHuman(chatId: number, from: TgFrom, lang: BotLang, opts?: { kind?: string }): Promise<Ticket | null> {
  const zh = lang === "zh";
  const botId = currentBot()?.id ?? BUILTIN_BOT_ID;
  const src = await lastSrc(from.id);
  const hub = await loadHub();
  const openLoad: Record<string, number> = {};
  for (const x of await listTickets()) if (x.status !== "resolved") openLoad[chatKey(x.supportBotId, x.supportChatId)] = (openLoad[chatKey(x.supportBotId, x.supportChatId)] ?? 0) + 1;
  const support = pickSupportChat(hub, botId, lang, openLoad);
  if (!support) {
    await handleHuman(chatId, from, src, lang);
    return null;
  }
  const existing = await activeTicketFor(botId, from.id);
  if (existing) {
    await sendText(
      chatId,
      zh
        ? `👤 你已经在人工工单 #${existing.id} 里了，直接在这里发消息或截图，客服会在这里回你。`
        : `👤 You're already in support ticket #${existing.id} — send your message or screenshot here and support will reply here.`,
      endKeyboard(existing.id, lang)
    );
    return existing;
  }
  const st = getBugState(from.id);
  const t = await createTicket({
    botId,
    uid: from.id,
    chatId,
    name: whoOf(from),
    username: from.username,
    src,
    lang,
    kind: opts?.kind ?? st?.kind ?? "human",
    fp: st?.fp,
    diag: st?.diag,
    note: st?.note,
    supportBotId: support.botId,
    supportChatId: support.chatId,
  });
  bugStates.delete(stateKey(from.id));

  const sBot = await botCtxById(support.botId, hub);
  let topicId: number | undefined;
  if (sBot && support.isForum) {
    const kindTag = t.kind !== "human" ? ` · ${kindLabel(t.kind, "zh")}` : "";
    const r = await withBot(sBot, () => tgCall("createForumTopic", { chat_id: support.chatId, name: `#${t.id} ${t.name}${kindTag}`.slice(0, 128) }));
    topicId = (r?.result as { message_thread_id?: number } | undefined)?.message_thread_id;
    if (topicId) {
      await updateTicket(t.id, (x) => {
        x.topicId = topicId;
      });
    }
  }
  const ticket: Ticket = { ...t, topicId };
  await postToTicket(ticket, await ticketCard(ticket), agentKeyboard(t.id));

  const online = inSupportHours(hub.support);
  const head = zh
    ? `👤 已为你接通人工客服（工单 #${t.id}）。\n直接在这里发文字、截图或文件，客服会在这里回复你，不用切换账号。`
    : `👤 You're connected to human support (ticket #${t.id}).\nSend text, screenshots or files right here — our team replies in this chat, no need to switch accounts.`;
  const off = online
    ? ""
    : zh
      ? `\n🌙 现在是非工作时间（客服在线 ${hub.support.hours}），消息已留好，上班后第一时间回你；急事先问我。`
      : `\n🌙 We're outside support hours (${hub.support.hours}); your message is queued and we'll reply as soon as we're back. Ask me anything meanwhile.`;
  await sendText(chatId, head + off, endKeyboard(t.id, lang));
  await trackTg("chatx_bot_ticket", { step: "open", id: t.id, kind: t.kind, src, uid: from.id, online });
  return ticket;
}

async function ticketCard(t: Ticket): Promise<string> {
  const lines = [
    `🎫 <b>工单 #${t.id}</b> · ${escHtml(t.kind === "human" ? "转人工" : `报障 / ${kindLabel(t.kind, "zh")}`)}`,
    `用户：${escHtml(t.name)}${t.username ? ` @${escHtml(t.username)}` : ""}（<code>${t.uid}</code>）`,
    `来源：<code>${escHtml(t.src)}</code> · 语言 ${t.lang}${t.botId !== BUILTIN_BOT_ID ? ` · bot ${escHtml(t.botId)}` : ""}`,
  ];
  if (t.fp) lines.push(`机器码：<code>${t.fp}</code>\n${escHtml(fpAgentLine(await summarizeFingerprint(t.fp)))}`);
  if (t.diag) lines.push(`诊断回执号：<code>${t.diag}</code>（管理员 bot 发 /diag ${t.diag} 取包）`);
  if (t.note) lines.push(`用户描述：${escHtml(t.note)}`);
  const hist = await recallHistory(t.chatId);
  if (hist.length) {
    lines.push("", "<b>最近对话</b>");
    for (const h of hist.slice(-6)) lines.push(`${h.role === "user" ? "🙋" : "🤖"} ${escHtml(h.content.slice(0, 200))}`);
  }
  lines.push("", t.topicId ? "在本话题里直接发消息 = 回复用户。/close 结案。" : "「回复」本工单的消息 = 回复用户。/close 结案。");
  return lines.join("\n");
}

export type RelayMsg = {
  message_id: number;
  chat: { id: number };
  text?: string;
  caption?: string;
  photo?: { file_id: string }[];
  document?: { file_id: string; file_name?: string; mime_type?: string };
  video?: { file_id: string };
  voice?: { file_id: string };
  audio?: { file_id: string };
};

function mediaOf(m: RelayMsg): { fileId: string; name: string; type: string } | null {
  if (m.photo?.length) return { fileId: m.photo[m.photo.length - 1].file_id, name: "photo.jpg", type: "image/jpeg" };
  if (m.document) return { fileId: m.document.file_id, name: m.document.file_name || "file", type: m.document.mime_type || "application/octet-stream" };
  if (m.video) return { fileId: m.video.file_id, name: "video.mp4", type: "video/mp4" };
  if (m.voice) return { fileId: m.voice.file_id, name: "voice.ogg", type: "audio/ogg" };
  if (m.audio) return { fileId: m.audio.file_id, name: "audio.mp3", type: "audio/mpeg" };
  return null;
}

/** 在两个会话之间搬一条消息：同一个 bot 用 copyMessage；不同 bot 文本重发、文件下载后重新上传。 */
async function relayMessage(fromBot: BotCtx, toBot: BotCtx, msg: RelayMsg, toChat: number | string, threadId: number | undefined, prefix: string): Promise<number | undefined> {
  const media = mediaOf(msg);
  if (fromBot.id === toBot.id && (media || !prefix)) {
    const caption = prefix && media ? (prefix + (msg.caption ?? "")).slice(0, 1024) : undefined;
    const r = await withBot(toBot, () => tgCall("copyMessage", { chat_id: toChat, from_chat_id: msg.chat.id, message_id: msg.message_id, message_thread_id: threadId, caption }));
    return msgIdOf(r);
  }
  if (media) {
    const buf = await withBot(fromBot, () => downloadTgFile(media.fileId, RELAY_MAX_BYTES));
    if (!buf) return undefined;
    const caption = (prefix + (msg.caption ?? "")).slice(0, 1024) || undefined;
    const r = await withBot(toBot, () => tgUpload("sendDocument", { chat_id: toChat, message_thread_id: threadId, caption }, { field: "document", name: media.name, data: Buffer.from(buf), type: media.type }));
    return msgIdOf(r);
  }
  if (msg.text) {
    const r = await withBot(toBot, () => tgCall("sendMessage", { chat_id: toChat, message_thread_id: threadId, text: prefix + msg.text, disable_web_page_preview: true }));
    return msgIdOf(r);
  }
  return undefined;
}

/**
 * 私聊消息（非命令）且用户有进行中的工单：转到客服群。
 * 返回 "claimed" = 客服已接手（AI 不再插话）；"open" = 客服还没接（AI 照常回答）；null = 没有工单。
 */
export async function mirrorUserMessage(from: TgFrom, msg: RelayMsg): Promise<"open" | "claimed" | null> {
  const bot = currentBot();
  if (!bot) return null;
  const t = await activeTicketFor(bot.id, from.id);
  if (!t) return null;
  const sBot = await botCtxById(t.supportBotId);
  if (!sBot) return null;
  const mid = await relayMessage(bot, sBot, msg, t.supportChatId, t.topicId, t.topicId ? "" : `🙋 #${t.id} ${t.name}：`);
  if (mid) await updateTicket(t.id, (x) => void x.msgIds.push(mid));
  return t.status === "claimed" ? "claimed" : "open";
}

export type AgentMsg = RelayMsg & { message_thread_id?: number; reply_to_message?: { message_id: number }; from?: TgFrom & { is_bot?: boolean } };

/** 客服群里的消息：属于某个工单就转给用户。返回 true 表示已处理。 */
export async function handleAgentMessage(supportChatId: string, msg: AgentMsg): Promise<boolean> {
  const sBot = currentBot();
  const agent = msg.from;
  if (!sBot || !agent || agent.is_bot) return false;
  const t = await ticketForSupportMessage(sBot.id, supportChatId, msg.message_thread_id, msg.reply_to_message?.message_id);
  if (!t) return false;
  const text = msg.text?.trim() ?? "";
  if (/^\/close\b/i.test(text)) {
    await agentAction(t, "r", agent);
    return true;
  }
  if (text.startsWith("/")) return true;
  if (t.status === "resolved") {
    await postToTicket(t, "ℹ️ 这个工单已结案，消息没有转给用户。");
    return true;
  }
  const uBot = await botCtxById(t.botId);
  if (!uBot) return true;
  const mid = await relayMessage(sBot, uBot, msg, t.chatId, undefined, t.lang === "zh" ? "👤 客服：" : "👤 Support: ");
  if (!mid) {
    await postToTicket(t, "⚠️ 没能转给用户（用户可能已屏蔽 bot，或文件超过 20MB）。");
    return true;
  }
  await updateTicket(t.id, (x) => {
    x.firstReplyAt ??= new Date().toISOString();
    if (x.status === "open") {
      x.status = "claimed";
      x.agent = whoOf(agent);
    }
  });
  if (!t.firstReplyAt) {
    await trackTg("chatx_bot_ticket", { step: "first_reply", id: t.id, src: t.src, wait_min: Math.round((Date.now() - Date.parse(t.createdAt)) / 60000) });
  }
  return true;
}

async function agentAction(t: Ticket, act: string, agent: TgFrom) {
  const zh = t.lang === "zh";
  const who = whoOf(agent);
  if (act === "c") {
    await updateTicket(t.id, (x) => {
      x.status = "claimed";
      x.agent = who;
    });
    await postToTicket(t, `✋ ${escHtml(who)} 已接单`);
    await notifyUser(t, zh ? `👤 客服 ${escHtml(who)} 已接入，稍等片刻。` : `👤 ${escHtml(who)} from support has joined — one moment.`);
    await trackTg("chatx_bot_ticket", { step: "claim", id: t.id, src: t.src });
    return;
  }
  if (t.status === "resolved") return;
  await updateTicket(t.id, (x) => {
    x.status = "resolved";
    x.resolvedAt = new Date().toISOString();
    x.agent ??= who;
  });
  await closeTopic(t);
  if (act === "r") {
    await postToTicket(t, `✅ ${escHtml(who)} 已结案`);
    await notifyUser(t, zh ? "✅ 客服已把这个问题标记为解决。解决了吗？" : "✅ Support marked this as solved. Did that fix it?", [
      [
        { text: zh ? "👍 解决了" : "👍 Yes", callback_data: `tk:y:${t.id}` },
        { text: zh ? "👎 还没有" : "👎 Not yet", callback_data: `tk:n:${t.id}` },
      ],
    ]);
    await trackTg("chatx_bot_ticket", { step: "resolve", id: t.id, src: t.src });
  } else {
    await postToTicket(t, `🤖 ${escHtml(who)} 已交回 AI`);
    await notifyUser(t, zh ? "🤖 人工客服已结束，接下来由我继续为你服务。需要人工随时点「人工客服」。" : "🤖 Human support has ended — I'll take it from here. Tap \"Human support\" any time.");
    await trackTg("chatx_bot_ticket", { step: "to_ai", id: t.id, src: t.src });
  }
}

async function userAction(t: Ticket, act: string, lang: BotLang) {
  const zh = lang === "zh";
  if (act === "u") {
    if (t.status === "resolved") return;
    await updateTicket(t.id, (x) => {
      x.status = "resolved";
      x.resolvedAt = new Date().toISOString();
    });
    await postToTicket(t, "🙋 用户结束了人工会话");
    await closeTopic(t);
    await notifyUser(t, zh ? "🤖 好的，回到 AI。有需要随时点「人工客服」。" : "🤖 OK, back to AI. Tap \"Human support\" any time.");
    await trackTg("chatx_bot_ticket", { step: "user_end", id: t.id, src: t.src });
  } else if (act === "y") {
    if (t.rating) return;
    await updateTicket(t.id, (x) => {
      x.rating = "y";
    });
    await postToTicket(t, "👍 用户确认已解决");
    await notifyUser(t, zh ? "🎉 感谢反馈！还有问题随时找我。" : "🎉 Thanks for the feedback! Reach out any time.");
    await trackTg("chatx_bot_ticket", { step: "rate", id: t.id, src: t.src, rating: "y" });
  } else if (act === "n") {
    if (t.status !== "resolved") return;
    await updateTicket(t.id, (x) => {
      x.rating = "n";
      x.status = "open";
      x.resolvedAt = undefined;
    });
    await closeTopic(t, true);
    await postToTicket(t, "👎 用户反馈<b>还没解决</b>，工单已重新打开", agentKeyboard(t.id));
    await notifyUser(t, zh ? "🙏 抱歉，已重新转给客服，直接在这里继续说明情况。" : "🙏 Sorry about that — it's back with support. Keep describing the issue here.", endKeyboard(t.id, lang));
    await trackTg("chatx_bot_ticket", { step: "rate", id: t.id, src: t.src, rating: "n" });
  }
}
