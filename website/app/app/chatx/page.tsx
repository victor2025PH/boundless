import type { Metadata } from "next";
import { isValidSrc } from "@/lib/attribution";
import ChatxMiniAppClient from "./client";

// ChatX 专用小程序（@ctx2026_bot 菜单键 / 键盘 web_app 按钮打开）：复用 /app 小程序壳与组件，
// 服务端先读 ?src= 传给客户端，避免首屏无归因再闪一次。
export const metadata: Metadata = {
  title: "智聊 ChatX · AI 全自动聊天 · Telegram 小程序",
  alternates: { canonical: "/app/chatx" },
  robots: { index: false, follow: false },
};

export default function ChatxMiniAppPage({ searchParams }: { searchParams?: { src?: string | string[] } }) {
  const raw = Array.isArray(searchParams?.src) ? searchParams?.src[0] : searchParams?.src;
  return <ChatxMiniAppClient initialSrc={isValidSrc(raw) ? raw : ""} />;
}
