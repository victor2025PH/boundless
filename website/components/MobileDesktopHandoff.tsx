"use client";

import { useState } from "react";
import { Check, Copy, Monitor, Send } from "lucide-react";
import { track } from "@/lib/track";
import { SITE_URL, TELEGRAM_HANDLE } from "@/lib/site";

/**
 * 移动端 → 桌面的交接卡（实施78 P1-2，2026-08-28）。
 *
 * 解决的问题：产品是 Windows 桌面端，而东南亚是移动优先——从 AI 答案 / 广告 / 社媒点进
 * 下载页的人很多在手机上，看完**当场断掉**，没有任何「回头在电脑上装」的桥（实施78 U9）。
 * 下载页此前零移动端处理（全站 grep 无 md:hidden 分支）。
 *
 * 刻意**不做「把链接发到你邮箱」**：站点当前没有任何发信依赖（package.json 无
 * nodemailer/resend/sendgrid），做那个按钮就是给用户一个我们兑现不了的承诺——
 * 宁可少一个入口，不要一个骗人的入口。邮件通道接上之后再补（见实施78 下一阶段）。
 *
 * 两个入口都**零新增基建、且一定能用**：
 *  ① 复制链接：clipboard API，失败回落 execCommand，再失败就把链接选中让用户手动复制；
 *  ② 发到 Telegram：走官方 share 深链 `t.me/share/url`，用户可选「收藏消息」——
 *     Telegram 桌面端会同步，等于把链接送到了他的电脑上。**不依赖我们的 bot 有 /start
 *     处理逻辑**（那条路径没验证过，做成死按钮反而更糟）。我们的用户 69% 的会话在
 *     Telegram 上，这个桥对他们比邮件自然。
 */
export default function MobileDesktopHandoff({ lang }: { lang: "zh" | "en" }) {
  const zh = lang === "zh";
  const [copied, setCopied] = useState(false);
  const url = `${SITE_URL}${zh ? "" : "/en"}/download/chatx`;

  async function copy() {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      setTimeout(() => setCopied(false), 2200);
    } catch {
      // 非安全上下文 / 旧浏览器：退回选中输入框，用户长按复制
      const el = document.getElementById("mdh-url") as HTMLInputElement | null;
      el?.select();
    }
    track("cta_click", { where: "mobile_handoff_copy" });
  }

  const shareHref = `https://t.me/share/url?url=${encodeURIComponent(url)}&text=${encodeURIComponent(
    zh ? "智聊 ChatX 下载（在电脑上打开）" : "ChatX download — open on your computer"
  )}`;

  return (
    <section className="px-5 pt-28 md:hidden">
      <div className="rounded-2xl border border-neon-cyan/25 bg-neon-cyan/[0.06] p-4">
        <div className="flex items-center gap-2 text-sm font-semibold text-white">
          <Monitor className="h-4 w-4 text-neon-cyan" />
          {zh ? "这是 Windows 电脑端软件" : "ChatX is a Windows desktop app"}
        </div>
        <p className="mt-1.5 text-xs leading-relaxed text-slate-400">
          {zh
            ? "手机上装不了。把链接带到电脑上继续——下面两种都行，几秒钟。"
            : "It can't install on a phone. Take the link to your computer — either way takes seconds."}
        </p>

        <input
          id="mdh-url"
          readOnly
          value={url}
          className="mt-3 w-full rounded-lg border border-white/10 bg-ink-950/60 px-3 py-2 text-xs text-slate-300"
        />

        <div className="mt-3 flex flex-wrap gap-2">
          <button
            onClick={copy}
            className="inline-flex items-center gap-1.5 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-4 py-2 text-xs font-medium text-ink-950"
          >
            {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
            {copied ? (zh ? "已复制" : "Copied") : zh ? "复制链接" : "Copy link"}
          </button>
          <a
            href={shareHref}
            target="_blank"
            rel="noreferrer"
            onClick={() => track("cta_click", { where: "mobile_handoff_telegram" })}
            className="inline-flex items-center gap-1.5 rounded-full border border-white/15 px-4 py-2 text-xs text-slate-200"
          >
            <Send className="h-3.5 w-3.5" />
            {zh ? "发到 Telegram（存收藏消息）" : "Send to Telegram (Saved Messages)"}
          </a>
        </div>

        <p className="mt-3 text-[11px] leading-relaxed text-slate-500">
          {zh
            ? `想让人帮你装？Telegram 联系 @${TELEGRAM_HANDLE}。`
            : `Want a hand installing it? Message @${TELEGRAM_HANDLE} on Telegram.`}
        </p>
      </div>
    </section>
  );
}
