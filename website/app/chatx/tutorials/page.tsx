import type { Metadata } from "next";
import Link from "next/link";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import TutorialPlaylist from "@/components/TutorialPlaylist";
import { SITE_URL } from "@/lib/site";
import { CHATX_TUTORIALS_OG, CHATX_TUTORIAL_COUNT, CHATX_TUTORIAL_TOTAL_SEC } from "@/lib/chatx-tutorials";
import { chatxTutorialsJsonLd } from "@/lib/chatx-tutorials-ld";

const MIN = Math.round(CHATX_TUTORIAL_TOTAL_SEC / 60);
const TITLE = `智聊 ChatX 视频教程 · ${CHATX_TUTORIAL_COUNT} 集约 ${MIN} 分钟学会 · 无界科技 BOUNDLESS`;
const DESC =
  "智聊 ChatX 官方视频教程：统一收件箱、扫码接入四平台、拟人互译、AI 拟稿与自动回复、人设工作室、知识库、克隆语音、工作目标、主动关怀、小智助手、风险护栏、充值额度——每集一个功能闭环，真机录屏，看完自动播下一集。";

export const metadata: Metadata = {
  title: TITLE,
  description: DESC,
  alternates: {
    canonical: "/chatx/tutorials",
    languages: { "zh-CN": "/chatx/tutorials", en: "/en/chatx/tutorials", "x-default": "/chatx/tutorials" },
  },
  openGraph: {
    title: `${CHATX_TUTORIAL_COUNT} 集约 ${MIN} 分钟学会智聊 · 智聊 ChatX 视频教程`,
    description: DESC,
    url: `${SITE_URL}/chatx/tutorials`,
    images: [{ url: CHATX_TUTORIALS_OG, width: 1200, height: 630 }],
  },
  twitter: { card: "summary_large_image", images: [CHATX_TUTORIALS_OG] },
};

const ld = chatxTutorialsJsonLd("zh");

export default function ChatxTutorialsPage() {
  return (
    <main className="relative min-h-screen">
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(ld) }} />
      <Navbar />
      <TutorialPlaylist lang="zh" />
      <section className="mx-auto max-w-7xl px-5 pb-20">
        <div className="grid gap-4 sm:grid-cols-3">
          <Link href="/download/chatx#install-guide" className="glass rounded-2xl border border-white/10 p-5 transition hover:border-neon-cyan/40">
            <div className="text-sm font-semibold text-white">文字版安装教程</div>
            <p className="mt-1 text-xs leading-relaxed text-slate-400">装前自查、SmartScreen 怎么点、SHA-256 校验，五步从下载到开始接待。</p>
            <span className="mt-3 inline-block text-xs text-neon-cyan">→ /download/chatx</span>
          </Link>
          <Link href="/pricing" className="glass rounded-2xl border border-white/10 p-5 transition hover:border-neon-cyan/40">
            <div className="text-sm font-semibold text-white">费率与充值档位</div>
            <p className="mt-1 text-xs leading-relaxed text-slate-400">标准翻译永久免费；Token 按量计费，用多少充多少，无月费无席位费。</p>
            <span className="mt-3 inline-block text-xs text-neon-cyan">→ 看价格</span>
          </Link>
          <Link href="/videos" className="glass rounded-2xl border border-white/10 p-5 transition hover:border-neon-cyan/40">
            <div className="text-sm font-semibold text-white">品牌片与效果演示</div>
            <p className="mt-1 text-xs leading-relaxed text-slate-400">3 分钟品牌片、同传 / 数字人真机实测与概念演示都在视频中心。</p>
            <span className="mt-3 inline-block text-xs text-neon-cyan">→ 视频中心</span>
          </Link>
        </div>
      </section>
      <Footer />
    </main>
  );
}
