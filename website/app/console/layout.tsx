// 集团控制台外壳：服务端查 sessions 表判定登录态（cookie 里是 session token）；
// 未登录渲染登录卡（users 空时为「初始化主账号」引导），已登录渲染页头 + 导航 + 页面。
// 页头显示当前用户名 + 角色徽章；「用户」导航入口仅 master 可见。
// 视觉（2026-08 对齐 BRAND_TOKENS）：深空 ink 底（slate 禁作暗底的品牌红线）+
// crown 皇冠资产强调色（tokens.json crown 组；与 /admin 青色系区分是实施09 既定决策）；
// 页头用 ∞ 品牌标（与登录页同源资产），文字冷灰仍走 slate 文字阶（允许项）。
import type { Metadata } from "next";
import { ShieldAlert, UserRound } from "lucide-react";
import { consoleConfigured, consoleUsersEmpty, getConsoleSessionUser } from "@/lib/console-auth";
import { RoleBadge } from "./parts";
import { ConsoleNav, ConsoleToaster, LoginCard, LogoutButton } from "./ui";

export const metadata: Metadata = {
  title: "无界 · 集团控制台 BOUNDLESS CONSOLE",
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

export default function ConsoleLayout({ children }: { children: React.ReactNode }) {
  const user = getConsoleSessionUser();
  return (
    // z-[120] + 不透明底：整体盖过营销站的全局特效/悬浮组件（AIChat z-[80] 等），控制台保持纯净
    <div className="relative z-[120] min-h-screen bg-ink-950 text-slate-200">
      {!user ? (
        <LoginCard configured={consoleConfigured()} usersEmpty={consoleUsersEmpty()} />
      ) : (
        <>
          <header className="sticky top-0 z-40 border-b border-crown-500/20 bg-ink-950/90 backdrop-blur">
            <div className="mx-auto max-w-6xl px-5">
              <div className="flex h-14 items-center justify-between gap-3">
                <div className="flex min-w-0 items-center gap-2.5">
                  {/* ∞ 品牌标与登录页同源（brand-assets 分发资产），替代此前的 lucide 皇冠图标 */}
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src="/brand/logos/boundless-mark-256.png"
                    alt="BOUNDLESS"
                    className="h-7 w-7 shrink-0 object-contain drop-shadow-[0_0_10px_rgba(240,160,16,0.35)]"
                    draggable={false}
                  />
                  <div className="min-w-0 leading-tight">
                    <span className="block truncate text-sm font-bold text-white">无界 · 集团控制台</span>
                    <span className="block text-[10px] font-semibold uppercase tracking-[0.18em] text-crown-500/80">
                      Boundless Console
                    </span>
                  </div>
                  <span className="ml-1 hidden shrink-0 items-center gap-1 rounded-full border border-crown-500/30 bg-crown-500/10 px-2.5 py-1 text-[11px] font-medium text-crown-300 sm:inline-flex">
                    <ShieldAlert className="h-3.5 w-3.5" />
                    皇冠资产 · 最小暴露
                  </span>
                </div>
                <div className="flex shrink-0 items-center gap-2.5">
                  <span className="flex items-center gap-1.5 text-xs text-slate-300">
                    <UserRound className="h-3.5 w-3.5 text-slate-500" />
                    <span className="max-w-[10rem] truncate font-medium" title={user.username}>
                      {user.username}
                    </span>
                    <RoleBadge role={user.role} compact />
                  </span>
                  <LogoutButton />
                </div>
              </div>
              <ConsoleNav showUsers={user.role === "master"} />
            </div>
          </header>
          <main className="mx-auto max-w-6xl px-5 py-6">{children}</main>
          <footer className="mx-auto max-w-6xl px-5 pb-8 pt-2 text-[11px] leading-relaxed text-slate-600">
            账本为影子镜像（JSON 主真相源 + 双写/回填）；本台管客户-订单-授权归属与台账，营销与获客内容仍在 /admin。
            实名账号 + RBAC（viewer 只读 / admin 运营 / master 主账号）；生产请独立设置 CONSOLE_KEY（仅剩脚本头通道与初始化用途）并配 IP 白名单。
          </footer>
        </>
      )}
      <ConsoleToaster />
    </div>
  );
}
