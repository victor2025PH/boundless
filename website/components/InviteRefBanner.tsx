"use client";

// 邀请落地横幅：好友分享链接 /download/chatx?ref=ZL-XXXXXX 打开时置顶提示
// 「装好后注册时填这个码，双方各得字符奖励」。码不落 cookie 不进服务端——
// 客户端注册走的是桌面 App 首启向导，用户要么记下码要么一键复制；
// 横幅只负责把码醒目地留在眼前 + 记一笔 invite_landing 漏斗事件。
// 形状不合法的 ?ref=（随手拼的参数）直接不渲染，防垃圾参数反射进页面。
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { ClipboardCheck, Copy, Gift } from "lucide-react";
import { track } from "@/lib/track";
import type { BrandLang } from "@/lib/brand";

export default function InviteRefBanner({ lang }: { lang: BrandLang }) {
  const zh = lang === "zh";
  const sp = useSearchParams();
  const code = useMemo(() => {
    const raw = (sp.get("ref") || "").trim().toUpperCase();
    return /^ZL-[0-9A-Z]{6}$/.test(raw) ? raw : "";
  }, [sp]);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (code) track("invite_landing", { code });
  }, [code]);

  if (!code) return null;

  function copy() {
    try {
      navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      /* 剪贴板不可用：码就在屏幕上 */
    }
  }

  return (
    <div className="mx-auto mt-24 -mb-14 w-full max-w-3xl px-4">
      <div className="flex flex-wrap items-center gap-3 rounded-2xl border border-emerald-400/25 bg-emerald-400/[0.06] px-4 py-3">
        <Gift className="h-5 w-5 shrink-0 text-emerald-400" />
        <div className="min-w-[200px] flex-1 text-sm leading-relaxed text-slate-200">
          {zh ? (
            <>
              好友邀请你使用智聊：安装后在首启向导「免费领 100 万字符」时填入邀请码
              <span className="mx-1 rounded bg-emerald-400/15 px-1.5 py-0.5 font-mono font-bold tracking-wider text-emerald-300">
                {code}
              </span>
              ，你们双方各得 100,000 字符奖励。
            </>
          ) : (
            <>
              A friend invited you to ChatX: enter invite code
              <span className="mx-1 rounded bg-emerald-400/15 px-1.5 py-0.5 font-mono font-bold tracking-wider text-emerald-300">
                {code}
              </span>
              when claiming your 1,000,000 free characters in the first-run wizard — you both
              get a 100,000-character bonus.
            </>
          )}
        </div>
        <button
          onClick={copy}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-emerald-400/30 px-3 py-1.5 text-xs font-semibold text-emerald-300 transition hover:bg-emerald-400/10"
        >
          {copied ? <ClipboardCheck className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
          {copied ? (zh ? "已复制" : "Copied") : zh ? "复制邀请码" : "Copy code"}
        </button>
      </div>
    </div>
  );
}
