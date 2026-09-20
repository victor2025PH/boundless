"use client";

import Image from "next/image";
import { Bot, Languages, LayoutGrid, Mic } from "lucide-react";
import Reveal from "./fx/Reveal";
import type { BrandLang } from "@/lib/brand";

/**
 * 真实客户端界面截图区（实施78 P0-5/P0-6）。
 *
 * 解决的问题：下载页此前没有一张产品界面图，等于让人只凭一段文字描述去下载一个
 * 442MB 的未签名安装包（实施78 用户视角 U4）。同一批素材也是 G2 / Capterra /
 * GetApp / Product Hunt 收录的**硬性要求**（3-6 张真实产品截图，问题 A3）。
 *
 * 素材由 `engines/chengjie/tools/gen_product_shots.py` 从**真实生产工作台**抓取：
 * 界面是真的，会话内容与联系人是演示数据（工具在 route 层强制覆盖身份字段，
 * 并在落盘前扫描画面可见文本、命中真实数据即拒绝落盘）。下方 caption 明写这一点
 * ——「真实界面 + 演示数据」是目录站接受的标准做法，但不能不说。
 *
 * 图片走 next/image：站点非静态导出，会按需转 AVIF/WebP；`/products/*` 在
 * next.config 里已配 1 年 immutable 缓存，故文件名带序号而不带内容 hash 也安全。
 */

const SHOTS = [
  {
    file: "01-unified-inbox.png",
    icon: LayoutGrid,
    title: { zh: "六平台统一收件箱", en: "One inbox, six platforms" },
    desc: {
      zh: "Telegram / WhatsApp / Messenger / LINE / Zalo / Instagram 的会话汇到一屏，未读、等待时长、自动化档位一眼可见。",
      en: "Telegram, WhatsApp, Messenger, LINE, Zalo and Instagram conversations in a single list — unread counts, waiting time and automation level at a glance.",
    },
    alt: {
      zh: "智聊 ChatX 桌面客户端的统一收件箱：左侧六个平台图标，中间跨平台会话列表，右侧 AI 回复台",
      en: "ChatX desktop unified inbox showing six messaging platforms in one conversation list with the AI reply desk on the right",
    },
  },
  {
    file: "02-multilingual-translation.png",
    icon: Languages,
    title: { zh: "客户母语进、你的语言出", en: "Customer's language in, yours out" },
    desc: {
      zh: "每条消息原文与译文双语对照，出站自动译回客户语言——印尼语、泰语、越南语客户不用你会他们的语言。",
      en: "Every message shows the original alongside the translation, and replies are translated back automatically — serve Indonesian, Thai or Vietnamese buyers without speaking the language.",
    },
    alt: {
      zh: "智聊 ChatX 消息流中的双语气泡：印尼语原文下方显示英文译文",
      en: "ChatX conversation view with bilingual message bubbles: Indonesian original text with the English translation underneath",
    },
  },
  {
    file: "03-ai-draft-review.png",
    icon: Bot,
    title: { zh: "AI 起草，你按发送", en: "AI drafts, you approve" },
    desc: {
      zh: "AI 按人设与对话上下文起草回复，标注风险档位与稿龄；坐席一键发送、改写或退回，也可放开为全自动。",
      en: "AI drafts replies in your persona and context, tagged with risk level and draft age. Send, edit or reject in one click — or let it run fully automatic.",
    },
    alt: {
      zh: "智聊 ChatX 的 AI 待审草稿卡：显示 L2 风险档位、稿龄与发送/退回/编辑按钮",
      en: "ChatX AI draft pending review card showing the L2 risk level, draft age and send / reject / edit actions",
    },
  },
  {
    file: "04-voice-and-toolbox.png",
    icon: Mic,
    title: { zh: "语音克隆与工具箱", en: "Voice cloning and toolbox" },
    desc: {
      zh: "用人设声音发语音消息，图片可 OCR 后翻译，多引擎译文对比、人设绑定、AI 出图都在右栏随手可用。",
      en: "Send voice messages in your persona's cloned voice, OCR-and-translate images, compare translation engines, bind personas and generate images — all from the side panel.",
    },
    alt: {
      zh: "智聊 ChatX 右侧工具箱：语音克隆与发送、图片翻译、多引擎对比、人设绑定、AI 出图",
      en: "ChatX side toolbox with voice clone and send, image translation, multi-engine comparison, persona binding and AI image generation",
    },
  },
] as const;

export default function ProductScreenshots({ lang }: { lang: BrandLang }) {
  const zh = lang === "zh";
  return (
    <Reveal className="mt-12">
      <div
        id="screenshots"
        className="glass scroll-mt-28 rounded-2xl border border-white/10 p-6 md:p-8"
      >
        <div className="flex items-center gap-2 text-lg font-semibold text-white">
          <LayoutGrid className="h-5 w-5 text-neon-cyan" />
          {zh ? "装之前，先看看它长什么样" : "See it before you install"}
        </div>
        <p className="mt-2 text-sm text-slate-400">
          {zh
            ? "以下是客户端的真实界面截图（Windows 版）。"
            : "Real screenshots from the shipping Windows client."}
        </p>

        <div className="mt-6 grid gap-6 md:grid-cols-2">
          {SHOTS.map((s) => (
            <figure key={s.file} className="group">
              <a
                href={`/products/screenshots/${s.file}`}
                target="_blank"
                rel="noreferrer"
                className="block overflow-hidden rounded-xl border border-white/10 bg-black/30"
                title={zh ? "点击查看原图" : "Open full size"}
              >
                <Image
                  src={`/products/screenshots/${s.file}`}
                  alt={s.alt[zh ? "zh" : "en"]}
                  width={3200}
                  height={2000}
                  sizes="(min-width: 768px) 45vw, 92vw"
                  className="h-auto w-full transition-transform duration-300 group-hover:scale-[1.02]"
                />
              </a>
              <figcaption className="mt-3">
                <div className="flex items-center gap-2 text-sm font-medium text-white">
                  <s.icon className="h-4 w-4 shrink-0 text-neon-cyan" />
                  {s.title[zh ? "zh" : "en"]}
                </div>
                <div className="mt-1 text-xs leading-relaxed text-slate-500">
                  {s.desc[zh ? "zh" : "en"]}
                </div>
              </figcaption>
            </figure>
          ))}
        </div>

        {/* 诚实声明：界面真、内容假。不写这句会让人以为聊天记录是真客户的。 */}
        <p className="mt-6 text-xs leading-relaxed text-slate-600">
          {zh
            ? "界面为真实客户端截图；为保护客户隐私，其中的联系人与会话内容均为演示数据。"
            : "The interface is captured from the real client. Contacts and conversation content are demo data, to protect customer privacy."}
        </p>
      </div>
    </Reveal>
  );
}
