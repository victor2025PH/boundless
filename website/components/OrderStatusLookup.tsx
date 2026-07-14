"use client";

import { useEffect, useState } from "react";
import { Check, CircleDashed, Search, XCircle } from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import { track } from "@/lib/track";

interface OrderInfo {
  id: string;
  status: "pending" | "paid" | "activated" | "cancelled";
  plan: string;
  period: string;
  pay_amount: number;
  t: string;
  paid_at: string | null;
  activated_at: string | null;
  /** 开通后返回的兑换码：客户在客户端「授权与激活」里输码即用。 */
  code: string | null;
}

/** 订单进度自助查询：输入单号 → 三态进度条（待付款 → 已到账 → 已开通）。支持 /order?check=AH-… 深链。 */
export default function OrderStatusLookup() {
  const { lang } = useLang();
  const zh = lang === "zh";
  const [id, setId] = useState("");
  const [busy, setBusy] = useState(false);
  const [info, setInfo] = useState<OrderInfo | null>(null);
  const [err, setErr] = useState("");
  const [codeCopied, setCodeCopied] = useState(false);

  const query = async (qid: string) => {
    const v = qid.trim().toUpperCase();
    if (!v) return;
    setBusy(true);
    setErr("");
    setInfo(null);
    try {
      const r = await fetch(`/api/order?id=${encodeURIComponent(v)}`);
      const j = await r.json();
      if (j?.ok) {
        setInfo(j as OrderInfo);
        track("order_status_check", { status: j.status });
      } else {
        setErr(zh ? "未找到该订单，请核对单号（形如 AH-20260711-XXXXXX）。" : "Order not found — check the ID (AH-20260711-XXXXXX).");
      }
    } catch {
      setErr(zh ? "查询失败，请稍后重试。" : "Lookup failed, try again later.");
    }
    setBusy(false);
  };

  // 深链 /order?check=AH-… 自动查询（付款后收藏链接即可随时看进度）
  useEffect(() => {
    const q = new URLSearchParams(window.location.search).get("check");
    if (q) {
      setId(q.toUpperCase());
      void query(q);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const steps = zh ? ["待付款", "已到账", "已开通"] : ["Pending", "Paid", "Activated"];
  const idx = info ? { pending: 0, paid: 1, activated: 2, cancelled: -1 }[info.status] : -2;

  return (
    <Reveal className="mt-14">
      <div className="glass rounded-2xl border border-white/10 p-6">
        <div className="flex items-center gap-2 font-semibold text-white">
          <Search className="h-5 w-5 text-neon-cyan" />
          {zh ? "查询订单进度" : "Check order status"}
        </div>
        <p className="mt-1 text-xs text-slate-500">
          {zh ? "输入下单时获得的单号；付款到账与开通后这里会实时更新。" : "Enter the order ID from checkout; payment and activation status update here."}
        </p>
        <div className="mt-4 flex flex-wrap gap-3">
          <input
            value={id}
            onChange={(e) => setId(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && query(id)}
            placeholder="AH-20260711-XXXXXX"
            className="w-full max-w-xs rounded-xl border border-white/10 bg-ink-950/60 px-4 py-2.5 font-mono text-sm text-white placeholder-slate-600 outline-none transition focus:border-neon-cyan/50"
          />
          <button
            onClick={() => query(id)}
            disabled={busy || !id.trim()}
            className="rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-2.5 text-sm font-medium text-ink-950 transition hover:opacity-90 disabled:opacity-40"
          >
            {busy ? (zh ? "查询中…" : "Checking…") : zh ? "查询" : "Check"}
          </button>
        </div>

        {err && <p className="mt-3 text-sm text-neon-pink">{err}</p>}

        {info && (
          <div className="mt-5">
            <div className="text-sm text-slate-300">
              <b className="text-white">{info.id}</b> · {info.plan} · {info.period === "annual" ? (zh ? "年付" : "annual") : zh ? "月付" : "monthly"}
              {info.pay_amount > 0 && (
                <>
                  {" · "}
                  {zh ? "应付 " : "due "}
                  <b className="text-neon-cyan">{info.pay_amount} USDT</b>
                </>
              )}
            </div>
            {info.status === "cancelled" ? (
              <div className="mt-3 flex items-center gap-2 text-sm text-slate-400">
                <XCircle className="h-4 w-4 text-neon-pink" />
                {zh ? "订单已取消。如有疑问请联系客服。" : "Order cancelled. Contact support if unexpected."}
              </div>
            ) : (
              <div className="mt-4 flex items-center">
                {steps.map((s, i) => (
                  <div key={s} className="flex flex-1 items-center last:flex-none">
                    <div className="flex flex-col items-center">
                      <span
                        className={`grid h-8 w-8 place-items-center rounded-full text-xs font-bold ${
                          i <= idx
                            ? "bg-gradient-to-r from-neon-cyan to-neon-violet text-ink-950"
                            : "border border-white/15 text-slate-500"
                        }`}
                      >
                        {i < idx ? <Check className="h-4 w-4" /> : i === idx ? <CircleDashed className="h-4 w-4" /> : i + 1}
                      </span>
                      <span className={`mt-1.5 text-xs ${i <= idx ? "text-white" : "text-slate-500"}`}>{s}</span>
                    </div>
                    {i < steps.length - 1 && (
                      <div className={`mx-2 mb-5 h-0.5 flex-1 rounded-full ${i < idx ? "bg-gradient-to-r from-neon-cyan to-neon-violet" : "bg-white/10"}`} />
                    )}
                  </div>
                ))}
              </div>
            )}
            {info.status === "pending" && (
              <p className="mt-3 text-xs text-slate-500">
                {zh
                  ? "请按「精确到小数」的应付金额转账——尾数是你的订单识别码，到账即可自动对上。"
                  : "Transfer the exact amount including decimals — the cents uniquely identify your order."}
              </p>
            )}
            {info.status === "activated" &&
              (info.code ? (
                <div className="mt-4 rounded-xl border border-emerald-400/30 bg-emerald-400/5 p-4">
                  <p className="text-xs font-medium text-emerald-400">
                    {zh ? "已开通 ✓ 你的专属授权码：" : "Activated ✓ Your license code:"}
                  </p>
                  <div className="mt-2 flex flex-wrap items-center gap-3">
                    <code className="max-h-24 max-w-full overflow-y-auto break-all rounded-lg bg-ink-950/70 px-4 py-2 font-mono text-xs font-bold text-neon-cyan">
                      {info.code.length > 120 ? `${info.code.slice(0, 100)}…（${zh ? "已截断，请用复制按钮" : "truncated — use Copy"}）` : info.code}
                    </code>
                    <button
                      onClick={() => {
                        navigator.clipboard?.writeText(info.code || "").then(() => {
                          setCodeCopied(true);
                          setTimeout(() => setCodeCopied(false), 2000);
                        });
                      }}
                      className="rounded-full border border-white/15 px-4 py-1.5 text-xs text-slate-300 transition hover:border-neon-cyan/50 hover:text-white"
                    >
                      {codeCopied ? (zh ? "已复制 ✓" : "Copied ✓") : zh ? "复制完整授权码" : "Copy full code"}
                    </button>
                  </div>
                  <p className="mt-2 text-xs text-slate-500">
                    {zh
                      ? `最快方式：打开客户端 → 「🔑 授权」→ 「订单号」框输入 ${info.id} → 点「在线激活」即刻生效。也可复制上方完整授权码手动粘贴激活。`
                      : `Fastest: open the client → License → enter ${info.id} in the Order ID field → Activate Online. Or copy the full code above and paste it manually.`}
                  </p>
                </div>
              ) : (
                <p className="mt-3 text-xs text-emerald-400">
                  {zh ? "已开通 ✓ 授权密钥已通过你留下的联系方式发送，请查收。" : "Activated ✓ Your license key was sent to your contact."}
                </p>
              ))}
          </div>
        )}
      </div>
    </Reveal>
  );
}
