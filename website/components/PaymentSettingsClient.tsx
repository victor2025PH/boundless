"use client";

import { useCallback, useEffect, useState } from "react";

// /admin/payment 表单本体：读写 /api/admin/payment（见 lib/payment-settings.ts）。
// 鉴权：优先复用 /admin 登录后的 admin_session httpOnly cookie（fetch 自动携带）；
// 未登录时可输入管理口令，仅存内存 state、以 ?key= 附带（同其他管理工具，不落 localStorage）。
// Stripe Secret Key 绝不在此页输入/展示——只能配在服务器环境变量 STRIPE_SECRET_KEY。

interface PaymentSettingsShape {
  usdt: { enabled: boolean; address: string };
  card: {
    enabled: boolean;
    provider: "stripe";
    publishableKey: string;
    currency: string;
    successUrl: string;
    cancelUrl: string;
  };
  updatedAt: string;
}

interface ApiResp {
  ok?: boolean;
  settings?: PaymentSettingsShape;
  cardSecretConfigured?: boolean;
  webhookConfigured?: boolean;
}

const inputCls =
  "mt-1.5 w-full rounded-xl border border-white/10 bg-ink-950/60 px-4 py-2.5 text-sm text-white placeholder-slate-600 outline-none transition focus:border-neon-cyan/50";

const apiUrl = (k: string) => `/api/admin/payment${k ? `?key=${encodeURIComponent(k)}` : ""}`;

export default function PaymentSettingsClient() {
  const [key, setKey] = useState("");
  const [gate, setGate] = useState<"checking" | "locked" | "open">("checking");
  const [s, setS] = useState<PaymentSettingsShape | null>(null);
  const [secretConfigured, setSecretConfigured] = useState(false);
  const [webhookConfigured, setWebhookConfigured] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const load = useCallback(async (k: string, silent = false) => {
    setBusy(true);
    if (!silent) setMsg(null);
    try {
      const r = await fetch(apiUrl(k), { cache: "no-store" });
      const j = (await r.json().catch(() => null)) as ApiResp | null;
      if (r.ok && j?.ok && j.settings) {
        setS(j.settings);
        setSecretConfigured(!!j.cardSecretConfigured);
        setWebhookConfigured(!!j.webhookConfigured);
        setGate("open");
      } else {
        setGate("locked");
        if (!silent) {
          setMsg({ ok: false, text: r.status === 401 ? "口令无效或会话已过期" : "加载失败，请稍后重试" });
        }
      }
    } catch {
      setGate("locked");
      if (!silent) setMsg({ ok: false, text: "网络错误，请稍后重试" });
    }
    setBusy(false);
  }, []);

  // 首次不带 key 试探：已在 /admin 登录过的浏览器凭 cookie 直通，否则落到口令闸门。
  useEffect(() => {
    void load("", true);
  }, [load]);

  const save = async () => {
    if (!s || busy) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await fetch(apiUrl(key.trim()), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ usdt: s.usdt, card: s.card }),
      });
      const j = (await r.json().catch(() => null)) as ApiResp | null;
      if (r.ok && j?.ok && j.settings) {
        setS(j.settings);
        setSecretConfigured(!!j.cardSecretConfigured);
        setWebhookConfigured(!!j.webhookConfigured);
        setMsg({ ok: true, text: "已保存，前台结算弹窗即时生效" });
      } else if (r.status === 401) {
        setGate("locked");
        setMsg({ ok: false, text: "口令无效或会话已过期，请重新验证" });
      } else {
        setMsg({ ok: false, text: "保存失败，请稍后重试" });
      }
    } catch {
      setMsg({ ok: false, text: "网络错误，请稍后重试" });
    }
    setBusy(false);
  };

  const patch = (fn: (draft: PaymentSettingsShape) => void) => {
    setS((prev) => {
      if (!prev) return prev;
      const next: PaymentSettingsShape = { ...prev, usdt: { ...prev.usdt }, card: { ...prev.card } };
      fn(next);
      return next;
    });
  };

  return (
    <div className="relative min-h-screen bg-ink-950 px-5 pb-24 pt-14">
      <div className="mx-auto max-w-2xl">
        <a href="/admin" className="text-xs text-slate-500 transition hover:text-neon-cyan">
          ← 返回控制台
        </a>
        <h1 className="mt-2 text-2xl font-bold text-white">支付渠道设置</h1>
        <p className="mt-1 text-sm text-slate-400">
          配置 /order 结算弹窗的收款方式：USDT（TRC20 链上转账）与银行卡（Stripe Checkout）。
        </p>

        {gate === "checking" && <p className="mt-10 text-sm text-slate-500">正在检查登录状态…</p>}

        {gate === "locked" && (
          <div className="glass mt-8 rounded-2xl border border-white/10 p-6">
            <div className="text-sm font-semibold text-white">管理员验证</div>
            <p className="mt-1 text-xs text-slate-500">
              已在 /admin 登录过的浏览器会自动放行；否则输入管理口令（仅存本页内存，不写入本地存储）。
            </p>
            <div className="mt-4 flex gap-2">
              <input
                type="password"
                value={key}
                onChange={(e) => setKey(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && key.trim() && load(key.trim())}
                placeholder="管理口令"
                className={`${inputCls} mt-0 flex-1`}
              />
              <button
                onClick={() => load(key.trim())}
                disabled={busy || !key.trim()}
                className="shrink-0 rounded-xl bg-gradient-to-r from-neon-cyan to-neon-violet px-5 py-2.5 text-sm font-medium text-ink-950 transition hover:opacity-90 disabled:opacity-50"
              >
                {busy ? "验证中…" : "进入"}
              </button>
            </div>
            {msg && !msg.ok && <p className="mt-3 text-xs text-rose-300">{msg.text}</p>}
          </div>
        )}

        {gate === "open" && s && (
          <>
            {/* ── USDT ── */}
            <section className="glass mt-8 rounded-2xl border border-white/10 p-6">
              <Toggle
                checked={s.usdt.enabled}
                onChange={(v) => patch((d) => (d.usdt.enabled = v))}
                label="USDT 收款（TRC20）"
                desc="关闭后结算弹窗不再展示 USDT 转账入口（历史订单不受影响）。"
              />
              <label className="mt-5 block">
                <span className="block text-xs text-slate-400">收款地址（TRC20）</span>
                <input
                  value={s.usdt.address}
                  onChange={(e) => patch((d) => (d.usdt.address = e.target.value))}
                  placeholder="T 开头的 TRC20 地址"
                  className={`${inputCls} font-mono`}
                />
                <span className="mt-1 block text-[11px] text-slate-600">
                  留空时前台回落到构建期环境变量 NEXT_PUBLIC_USDT_ADDR；两者都空则引导客户联系客服取地址。
                </span>
              </label>
            </section>

            {/* ── 银行卡 / Stripe ── */}
            <section className="glass mt-5 rounded-2xl border border-white/10 p-6">
              <Toggle
                checked={s.card.enabled}
                onChange={(v) => patch((d) => (d.card.enabled = v))}
                label="银行卡收款（Stripe Checkout）"
                desc="启用且服务器已配 Secret Key 时，结算弹窗出现「银行卡」入口；条件不满足前台自动回落 USDT。"
              />

              <div
                className={`mt-4 rounded-xl border px-3 py-2.5 text-xs leading-relaxed ${
                  secretConfigured
                    ? "border-emerald-400/30 bg-emerald-400/10 text-emerald-200"
                    : "border-amber-400/30 bg-amber-400/10 text-amber-200"
                }`}
              >
                {secretConfigured ? (
                  <>✓ Stripe Secret Key 已配置（服务器环境变量 STRIPE_SECRET_KEY）。</>
                ) : (
                  <>
                    ⚠ Stripe Secret Key 未配置：请在服务器 .env.local 设置 STRIPE_SECRET_KEY=sk_… 并重启服务。
                    密钥只放服务器环境变量——绝不在浏览器输入、存储或传输；未配置时前台银行卡入口不可用，自动回落
                    USDT。
                  </>
                )}
              </div>

              <div
                className={`mt-2 rounded-xl border px-3 py-2.5 text-xs leading-relaxed ${
                  webhookConfigured
                    ? "border-emerald-400/30 bg-emerald-400/10 text-emerald-200"
                    : "border-amber-400/30 bg-amber-400/10 text-amber-200"
                }`}
              >
                {webhookConfigured ? (
                  <>✓ Webhook 对账已启用（STRIPE_WEBHOOK_SECRET）：客户付款即自动到账，无需回跳官网。</>
                ) : (
                  <>
                    ⚠ Webhook 未配置：当前只能靠付款后回跳对账，客户关掉页面会漏单。配置方法：Stripe 后台
                    Developers → Webhooks → Add endpoint，URL 填 <b>https://bd2026.cc/api/payment/webhook</b>，事件勾选
                    checkout.session.completed / async_payment_succeeded / async_payment_failed，把生成的签名密钥
                    （whsec_…）写入服务器 .env.local 的 STRIPE_WEBHOOK_SECRET 并重启。
                  </>
                )}
              </div>

              <div className="mt-5 grid gap-4">
                <div className="flex items-baseline justify-between gap-4 border-b border-dashed border-white/5 pb-2 text-sm">
                  <span className="text-slate-500">支付服务商</span>
                  <b className="font-mono text-white">stripe（固定）</b>
                </div>
                <label className="block">
                  <span className="block text-xs text-slate-400">Publishable Key（公开密钥）</span>
                  <input
                    value={s.card.publishableKey}
                    onChange={(e) => patch((d) => (d.card.publishableKey = e.target.value))}
                    placeholder="pk_live_… / pk_test_…"
                    className={`${inputCls} font-mono`}
                  />
                  <span className="mt-1 block text-[11px] text-slate-600">
                    pk_ 开头的可公开密钥（与 sk_ 私钥不同），可安全下发浏览器。
                  </span>
                </label>
                <label className="block">
                  <span className="block text-xs text-slate-400">结算币种</span>
                  <input
                    value={s.card.currency}
                    onChange={(e) =>
                      patch((d) => (d.card.currency = e.target.value.toUpperCase().replace(/[^A-Z]/g, "").slice(0, 8)))
                    }
                    placeholder="USD"
                    className={`${inputCls} font-mono uppercase`}
                  />
                  <span className="mt-1 block text-[11px] text-slate-600">ISO 货币码，默认 USD。</span>
                </label>
                <label className="block">
                  <span className="block text-xs text-slate-400">支付成功回跳 URL（选填）</span>
                  <input
                    value={s.card.successUrl}
                    onChange={(e) => patch((d) => (d.card.successUrl = e.target.value))}
                    placeholder="https://…（留空 = 站内 /order?check=<订单号>）"
                    className={`${inputCls} font-mono`}
                  />
                </label>
                <label className="block">
                  <span className="block text-xs text-slate-400">取消支付回跳 URL（选填）</span>
                  <input
                    value={s.card.cancelUrl}
                    onChange={(e) => patch((d) => (d.card.cancelUrl = e.target.value))}
                    placeholder="https://…（留空 = 站内 /order）"
                    className={`${inputCls} font-mono`}
                  />
                </label>
              </div>
            </section>

            {/* ── 保存 ── */}
            <div className="mt-6 flex flex-wrap items-center gap-3">
              <button
                onClick={save}
                disabled={busy}
                className="rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-2.5 text-sm font-medium text-ink-950 transition hover:opacity-90 disabled:opacity-50"
              >
                {busy ? "保存中…" : "保存设置"}
              </button>
              {msg && (
                <span className={`text-sm ${msg.ok ? "text-emerald-300" : "text-rose-300"}`}>{msg.text}</span>
              )}
              {s.updatedAt && (
                <span className="ml-auto text-[11px] text-slate-600">
                  上次保存 {new Date(s.updatedAt).toLocaleString("zh-CN")}
                </span>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function Toggle({
  checked,
  onChange,
  label,
  desc,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  desc?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
      className="flex w-full items-center justify-between gap-4 text-left"
    >
      <span className="min-w-0">
        <span className="block text-sm font-semibold text-white">{label}</span>
        {desc && <span className="mt-0.5 block text-xs text-slate-500">{desc}</span>}
      </span>
      <span
        className={`relative h-6 w-11 shrink-0 rounded-full border transition ${
          checked ? "border-neon-cyan/60 bg-neon-cyan/80" : "border-white/15 bg-white/5"
        }`}
      >
        <span
          className={`absolute top-1/2 h-4 w-4 -translate-y-1/2 rounded-full transition-all ${
            checked ? "left-6 bg-ink-950" : "left-1 bg-slate-400"
          }`}
        />
      </span>
    </button>
  );
}
