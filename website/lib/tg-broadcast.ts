import { readFile, stat } from "fs/promises";
import path from "path";
import { TELEGRAM_CHANNEL, TELEGRAM_GROUP, BOT_URL, SITE_URL, siteUtmLink, miniappUtmLink } from "./site";
import { buildOverviewPost } from "./catalog-posts";

export type BroadcastTarget = "channel" | "group" | "both";
export interface BroadcastResult {
  chat: string;
  ok: boolean;
  error?: string;
  messageId?: number;
}

// 「官网」按钮带 UTM 深链：medium=发布位置(channel/group)，campaign=帖子标识，
// 让后台能算出「哪条帖子带来多少官网会话和留资」。
function mediumFor(chat: string): string {
  return chat === `@${TELEGRAM_GROUP}` ? "group" : "channel";
}

/** site 可选深链：让「官网」按钮直达特定落地页（如 /ko/voice），label 换成对应文案。 */
interface SiteButton {
  path?: string;
  label?: string;
}

const SITE_HOST = SITE_URL.replace(/^https?:\/\//, "").replace(/\/$/, "");

function richButtons(medium: string, campaign: string, site?: SiteButton) {
  /* 客服走 bot 深链（t.me 域内零确认弹窗）：bot 秒回欢迎 = 客服先开口，转人工有兜底通知 */
  const csPayload = `cs_${(campaign || medium).replace(/[^A-Za-z0-9_-]/g, "")}`.slice(0, 64);
  return {
    inline_keyboard: [
      [
        /* 官网是外链，平台确认弹窗关不掉——文案带域名，让弹窗内容与按钮一致，降低戒备 */
        { text: site?.label ?? `🌐 官网 ${SITE_HOST}`, url: siteUtmLink(medium, campaign, site?.path ?? "/") },
        { text: "📱 小程序", url: miniappUtmLink(campaign) },
      ],
      [
        { text: "🤖 机器人", url: BOT_URL },
        { text: "👤 客服", url: `${BOT_URL}?start=${csPayload}` },
      ],
    ],
  };
}

async function callApi(token: string, method: string, body: Record<string, unknown>) {
  const res = await fetch(`https://api.telegram.org/bot${token}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

async function postTo(
  token: string,
  chat: string,
  text: string,
  withButton: boolean,
  campaign = "",
  site?: SiteButton
): Promise<BroadcastResult> {
  const body: Record<string, unknown> = {
    chat_id: chat,
    text,
    parse_mode: "HTML",
    disable_web_page_preview: true,
  };
  if (withButton) body.reply_markup = richButtons(mediumFor(chat), campaign, site);
  try {
    const data = await callApi(token, "sendMessage", body);
    return {
      chat,
      ok: Boolean(data?.ok),
      error: data?.ok ? undefined : data?.description,
      messageId: data?.result?.message_id,
    };
  } catch (e) {
    return { chat, ok: false, error: String(e) };
  }
}

// photo can be an https URL (Telegram fetches) or a local file path (we upload via multipart).
// Multipart upload is preferred for large assets to avoid Telegram's fetch timeout.
async function photoTo(
  token: string,
  chat: string,
  photo: string,
  caption: string,
  withButton: boolean,
  campaign = "",
  site?: SiteButton
): Promise<BroadcastResult> {
  const isUrl = /^https?:\/\//i.test(photo);
  try {
    let data: { ok?: boolean; description?: string; result?: { message_id?: number } };
    if (isUrl) {
      const body: Record<string, unknown> = { chat_id: chat, photo, caption, parse_mode: "HTML" };
      if (withButton) body.reply_markup = richButtons(mediumFor(chat), campaign, site);
      data = await callApi(token, "sendPhoto", body);
    } else {
      const buf = await readFile(photo);
      const form = new FormData();
      form.append("chat_id", chat);
      form.append("caption", caption);
      form.append("parse_mode", "HTML");
      if (withButton) form.append("reply_markup", JSON.stringify(richButtons(mediumFor(chat), campaign, site)));
      form.append("photo", new Blob([new Uint8Array(buf)]), path.basename(photo));
      const res = await fetch(`https://api.telegram.org/bot${token}/sendPhoto`, {
        method: "POST",
        body: form,
      });
      data = await res.json();
    }
    return {
      chat,
      ok: Boolean(data?.ok),
      error: data?.ok ? undefined : data?.description,
      messageId: data?.result?.message_id,
    };
  } catch (e) {
    return { chat, ok: false, error: String(e) };
  }
}

export function targetChats(target: BroadcastTarget): string[] {
  const out: string[] = [];
  if (target === "channel" || target === "both") out.push(`@${TELEGRAM_CHANNEL}`);
  if (target === "group" || target === "both") out.push(`@${TELEGRAM_GROUP}`);
  return out;
}

/** Send a message to the channel/group. Returns per-target results. */
export async function broadcastMessage(opts: {
  text: string;
  target: BroadcastTarget;
  withButton: boolean;
  campaign?: string;
  sitePath?: string;
  siteLabel?: string;
}): Promise<{ ok: boolean; results: BroadcastResult[] }> {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) return { ok: false, results: [{ chat: "-", ok: false, error: "no_bot_token" }] };
  const chats = targetChats(opts.target);
  const site = opts.sitePath || opts.siteLabel ? { path: opts.sitePath, label: opts.siteLabel } : undefined;
  const results = await Promise.all(
    chats.map((c) => postTo(token, c, opts.text, opts.withButton, opts.campaign ?? "", site))
  );
  return { ok: results.length > 0 && results.every((r) => r.ok), results };
}

/** Send a photo with caption + buttons to the channel/group. */
export async function broadcastPhoto(opts: {
  photo: string;
  caption: string;
  target: BroadcastTarget;
  withButton: boolean;
  campaign?: string;
  sitePath?: string;
  siteLabel?: string;
}): Promise<{ ok: boolean; results: BroadcastResult[] }> {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) return { ok: false, results: [{ chat: "-", ok: false, error: "no_bot_token" }] };
  const chats = targetChats(opts.target);
  const site = opts.sitePath || opts.siteLabel ? { path: opts.sitePath, label: opts.siteLabel } : undefined;
  const results = await Promise.all(
    chats.map((c) => photoTo(token, c, opts.photo, opts.caption, opts.withButton, opts.campaign ?? "", site))
  );
  return { ok: results.length > 0 && results.every((r) => r.ok), results };
}

// ── 多级降压视频广播（2026-08-07）：日更 feed 此前只有「URL sendVideo→文字」两级，
// Telegram URL 拉取上限 20MB，长教学片必然降级成纯文字帖（体验大损）。
// 新阶梯：①本地文件 ≤49MB → multipart 上传真视频帖（bot 上限 50MB；同晨目录帖
// multipart 图片上传已实证此运行时通路）②超限/失败 → 海报图+链接帖 ③最后才纯文字。
const MEDIA_ROOT = process.env.MEDIA_FEED_DIR || "/var/www/media/feed";
const TG_VIDEO_MAX_MB = 49;

async function sendVideoFile(
  token: string,
  chat: string,
  filePath: string,
  caption: string,
  keyboard: ReturnType<typeof richButtons>
): Promise<BroadcastResult> {
  try {
    const buf = await readFile(filePath);
    const form = new FormData();
    form.append("chat_id", chat);
    form.append("caption", caption);
    form.append("parse_mode", "HTML");
    form.append("supports_streaming", "true");
    form.append("reply_markup", JSON.stringify(keyboard));
    form.append("video", new Blob([new Uint8Array(buf)]), path.basename(filePath));
    const res = await fetch(`https://api.telegram.org/bot${token}/sendVideo`, {
      method: "POST",
      body: form,
    });
    const data = await res.json();
    return {
      chat,
      ok: Boolean(data?.ok),
      error: data?.ok ? undefined : data?.description,
      messageId: data?.result?.message_id,
    };
  } catch (e) {
    return { chat, ok: false, error: String(e) };
  }
}

/** 站内 /media/feed/* 路径 → 服务器本地文件路径；外链/其它路径返回 null。 */
function mediaLocalPath(src: string): string | null {
  if (!src.startsWith("/media/feed/")) return null;
  return path.join(MEDIA_ROOT, path.basename(src));
}

/** 日更/品牌视频的智能广播阶梯：multipart 真视频 → 海报+链接 → URL 视频 → 文字。 */
export async function broadcastVideoSmart(opts: {
  src: string; // 站内路径（/media/feed/..）或 https URL
  videoUrl: string; // 绝对 URL（阶梯后段与文字帖用）
  posterUrl?: string; // 海报绝对 URL（photo 帖用，Telegram 自取 ≤5MB 足够）
  caption: string;
  campaign?: string;
  sitePath?: string;
  siteLabel?: string;
}): Promise<BroadcastResult> {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) return { chat: "-", ok: false, error: "no_bot_token" };
  const chat = `@${TELEGRAM_CHANNEL}`;
  const buttons = richButtons("channel", opts.campaign ?? "video-feed", {
    path: opts.sitePath ?? "/videos",
    label: opts.siteLabel ?? "🎬 更多演示",
  });

  const local = mediaLocalPath(opts.src);
  if (local) {
    try {
      const sizeMB = (await stat(local)).size / 1048576;
      if (sizeMB <= TG_VIDEO_MAX_MB) {
        const up = await sendVideoFile(token, chat, local, opts.caption, buttons);
        if (up.ok) return up;
      }
    } catch {
      /* 本地不可读则继续走 URL 阶梯 */
    }
    // 超限或上传失败：海报图 + 链接（比纯文字体面一级）
    if (opts.posterUrl) {
      const cap = `${opts.caption}\n\n▶️ ${opts.videoUrl}`;
      const ph = await photoTo(token, chat, opts.posterUrl, cap.slice(0, 1024), true, opts.campaign ?? "video-feed");
      if (ph.ok) return ph;
    }
  }
  return broadcastVideoToChannel(opts);
}

/** Send a video (https URL ≤20MB — Telegram fetches it) with caption + buttons to the channel.
 *  URL >20MB 或发送失败时降级为带链接的文字帖，保证每日发布不断档。 */
export async function broadcastVideoToChannel(opts: {
  videoUrl: string;
  caption: string;
  campaign?: string;
  sitePath?: string;
  siteLabel?: string;
}): Promise<BroadcastResult> {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) return { chat: "-", ok: false, error: "no_bot_token" };
  const chat = `@${TELEGRAM_CHANNEL}`;
  const buttons = richButtons("channel", opts.campaign ?? "video-feed", {
    path: opts.sitePath ?? "/videos",
    label: opts.siteLabel ?? "🎬 更多演示",
  });
  try {
    const data = await callApi(token, "sendVideo", {
      chat_id: chat,
      video: opts.videoUrl,
      caption: opts.caption,
      parse_mode: "HTML",
      supports_streaming: true,
      reply_markup: buttons,
    });
    if (data?.ok) return { chat, ok: true, messageId: data?.result?.message_id };
    // 降级：文字 + 链接（视频过大 / Telegram 拉取超时等）
    const fallback = await callApi(token, "sendMessage", {
      chat_id: chat,
      text: `${opts.caption}\n\n▶️ ${opts.videoUrl}`,
      parse_mode: "HTML",
      reply_markup: buttons,
    });
    return {
      chat,
      ok: Boolean(fallback?.ok),
      error: fallback?.ok ? undefined : `video:${data?.description}; text:${fallback?.description}`,
      messageId: fallback?.result?.message_id,
    };
  } catch (e) {
    return { chat, ok: false, error: String(e) };
  }
}

export async function pinMessage(chat: string, messageId: number, silent = true): Promise<boolean> {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) return false;
  try {
    const data = await callApi(token, "pinChatMessage", {
      chat_id: chat,
      message_id: messageId,
      disable_notification: silent,
    });
    return Boolean(data?.ok);
  } catch {
    return false;
  }
}

export async function deleteMessage(chat: string, messageId: number): Promise<boolean> {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) return false;
  try {
    const data = await callApi(token, "deleteMessage", { chat_id: chat, message_id: messageId });
    return Boolean(data?.ok);
  } catch {
    return false;
  }
}

// ── 频道 / 群 品牌信息 ────────────────────────────────────────────────
// 显示名 + 简介。需要 Bot 是该频道/群的管理员（有「修改群信息」权限）。
// username（@hykj7 / @hykjz）属平台实体，不在此改动（保留以免断历史外链）。

export const CHANNEL_BRAND = {
  channel: {
    title: "无界科技 BOUNDLESS · 官方频道",
    description:
      "无界科技官方频道 · 让沟通，无界。" +
      "主推 💬智聊 ChatX：统一收件箱＋AI 自动成交＋拟人互译，下载即免费开始。" +
      "另有 🎯真机获客 🎭换脸 🎙克隆声音 🎬直播分身 🔐私有部署。" +
      "新功能 · 限时优惠第一时间发布 · USDT 结算。官网与客服见置顶。",
  },
  group: {
    title: "无界科技 · 交流群",
    description:
      "无界科技官方交流群 · 主聊 💬智聊 ChatX（收件箱/AI 成交/翻译），也聊换脸、克隆声音、直播分身。" +
      "提问、领试用、同行交流。@小界 或点客服随时响应；广告与刷屏将被移除。",
  },
} as const;

// 设置频道/群头像。需要 Bot 是管理员且有「修改群信息」权限。photo 为本地文件路径，multipart 上传。
async function setChatPhoto(token: string, chat: string, photoPath: string): Promise<BroadcastResult> {
  try {
    const buf = await readFile(photoPath);
    const form = new FormData();
    form.append("chat_id", chat);
    form.append("photo", new Blob([new Uint8Array(buf)]), path.basename(photoPath));
    const res = await fetch(`https://api.telegram.org/bot${token}/setChatPhoto`, {
      method: "POST",
      body: form,
    });
    const data = await res.json();
    return { chat: `${chat} (photo)`, ok: Boolean(data?.ok), error: data?.ok ? undefined : data?.description };
  } catch (e) {
    return { chat: `${chat} (photo)`, ok: false, error: String(e) };
  }
}

async function setChatMeta(
  token: string,
  chat: string,
  title: string,
  description: string
): Promise<BroadcastResult> {
  try {
    // setChatDescription 上限约 255 字符；标题上限 128。
    const t = await callApi(token, "setChatTitle", { chat_id: chat, title: title.slice(0, 128) });
    const d = await callApi(token, "setChatDescription", {
      chat_id: chat,
      description: description.slice(0, 255),
    });
    // 幂等：标题/简介未变化时 Telegram 返回 "...is not modified"，视为成功。
    const tDesc = String((t as { description?: string })?.description || "");
    const dDesc = String((d as { description?: string })?.description || "");
    const okT = Boolean(t?.ok) || /is not modified/i.test(tDesc);
    const okD = Boolean(d?.ok) || /is not modified/i.test(dDesc);
    const ok = okT && okD;
    return {
      chat,
      ok,
      error: ok ? undefined : tDesc || dDesc || "set_meta_failed",
    };
  } catch (e) {
    return { chat, ok: false, error: String(e) };
  }
}

/** 无界科技品牌头像（深底圆裁友好）本地路径，供频道/群 setChatPhoto 使用。
 *  由 brand-assets 管线（build_brand_assets.py → sync_brand_targets.py）同步 boundless-avatar.png。 */
function brandAvatarPath(): string {
  return path.join(process.cwd(), "public", "brand", "logos", "boundless-avatar.png");
}

/** Set channel & group display name + description (+ avatar, + pinned overview).
 *  Bot must be an admin of each chat with "change info" rights for title/description/photo.
 *  Failures are returned per-target, never thrown. */
export async function setupChannels(opts?: {
  pinOverview?: boolean;
  setPhoto?: boolean;
  forcePin?: boolean;
}): Promise<{
  ok: boolean;
  results: BroadcastResult[];
}> {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) return { ok: false, results: [{ chat: "-", ok: false, error: "no_bot_token" }] };

  const results: BroadcastResult[] = [];
  results.push(
    await setChatMeta(
      token,
      `@${TELEGRAM_CHANNEL}`,
      CHANNEL_BRAND.channel.title,
      CHANNEL_BRAND.channel.description
    )
  );
  results.push(
    await setChatMeta(
      token,
      `@${TELEGRAM_GROUP}`,
      CHANNEL_BRAND.group.title,
      CHANNEL_BRAND.group.description
    )
  );

  if (opts?.setPhoto !== false) {
    const avatar = brandAvatarPath();
    results.push(await setChatPhoto(token, `@${TELEGRAM_CHANNEL}`, avatar));
    results.push(await setChatPhoto(token, `@${TELEGRAM_GROUP}`, avatar));
  }

  if (opts?.pinOverview !== false) {
    // 幂等：若频道当前置顶已是本概览贴（按 caption 首行签名识别），默认跳过，避免重跑刷屏。
    // 频道消息没有 from.id（以 sender_chat 归属频道），故用内容签名而非发送者判断。
    // forcePin=true 可强制重发并置顶。
    const overview = buildOverviewPost("zh");
    const sig = overview.caption.split("\n")[0].replace(/<[^>]+>/g, "").trim();
    let alreadyPinned = false;
    if (!opts?.forcePin) {
      try {
        const chat = await callApi(token, "getChat", { chat_id: `@${TELEGRAM_CHANNEL}` });
        const pinned = (chat as { result?: { pinned_message?: { photo?: unknown; caption?: string } } })
          ?.result?.pinned_message;
        alreadyPinned = Boolean(
          pinned?.photo && typeof pinned?.caption === "string" && sig && pinned.caption.includes(sig)
        );
      } catch {
        /* 查询失败则退回到正常发布逻辑 */
      }
    }

    if (alreadyPinned) {
      results.push({ chat: `@${TELEGRAM_CHANNEL} (overview: already pinned, skipped)`, ok: true });
    } else {
      const posted = await photoTo(token, `@${TELEGRAM_CHANNEL}`, overview.imagePath, overview.caption, true, "overview");
      results.push({ ...posted, chat: `@${TELEGRAM_CHANNEL} (overview)` });
      if (posted.ok && posted.messageId) {
        await pinMessage(`@${TELEGRAM_CHANNEL}`, posted.messageId, true);
      }
    }
  }

  return { ok: results.every((r) => r.ok), results };
}
