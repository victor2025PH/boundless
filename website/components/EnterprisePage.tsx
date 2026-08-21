"use client";

// /enterprise 企业服务独立页（2026-08-21 实施50 P4）：
// 定位=「销售件 + 线索捕获」——/pricing 企业双卡只是路标，真正的资质叙事、
// 形态对比、实施流程、FAQ 与站内表单都在这里（企业买家不一定用 Telegram，
// 外跳 TG 之外必须有一条零门槛的留资通道）。
// 契约：表单打既有 POST /api/lead（蜜罐/去重/管理员 TG 通知/console 列表全复用，
// 后端零改动）；interest 前缀 "enterprise:" 供 /console/leads 一眼分流；
// 两形态数据取 lib/chatx-pricing.ts::ENTERPRISE_TRACKS 单源（与 /pricing 同一份）。
import { useEffect, useState } from "react";
import Link from "next/link";
import {
  ArrowRight,
  Building2,
  Cable,
  CheckCircle2,
  ClipboardCheck,
  Database,
  GraduationCap,
  Loader2,
  Lock,
  Rocket,
  Send,
  Server,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import { track } from "@/lib/track";
import { getLeadUtm } from "@/lib/attribution";
import { CONTACT_URL } from "@/lib/site";
import { ENTERPRISE_TRACKS } from "@/lib/chatx-pricing";
import { ENTERPRISE_FAQ } from "@/lib/enterpriseContent";
import { isValidContact } from "./LeadForm";

/* ── 页面本地数据（叙事层；报价事实仍在 chatx-pricing 单源）───────────────── */

const TRACK_EXTRA: Record<string, {
  fit: { zh: string; en: string };
  deliver: { zh: string[]; en: string[] };
}> = {
  "enterprise-coop": {
    fit: {
      zh: "适合：团队用量大、希望云端省心，数据可走我们托管的组织",
      en: "For teams with heavy usage that prefer a managed cloud setup",
    },
    deliver: {
      zh: ["企业账号与子账号体系", "年框协议价 + 月结对公发票", "专属客户成功群 · 优先支持 SLA", "季度用量报告与优化建议"],
      en: ["Org account with sub-accounts", "Annual frame pricing + monthly corporate invoicing", "Dedicated success channel · priority SLA", "Quarterly usage reviews"],
    },
  },
  "private-deploy": {
    fit: {
      zh: "适合：数据必须留在内网、有合规硬性要求、或想用自有 GPU 摊平成本的组织",
      en: "For orgs whose data must stay on-prem, with hard compliance needs, or with their own GPUs",
    },
    deliver: {
      zh: ["整套引擎装进你的服务器 / 内网 GPU", "本地模型 Token 不限量（自有算力零边际成本）", "实施 + 团队培训 + 验收", "年授权维保：升级通道 · 远程支持 SLA"],
      en: ["Full engine on your servers / on-prem GPUs", "Unlimited local-model tokens (zero marginal cost on your GPUs)", "Setup + team training + acceptance", "Annual license & care: upgrades · remote support SLA"],
    },
  },
};

const COMPARE_ROWS: { label: { zh: string; en: string }; coop: { zh: string; en: string }; deploy: { zh: string; en: string } }[] = [
  {
    label: { zh: "部署位置", en: "Where it runs" },
    coop: { zh: "我们托管（云端 + 桌面客户端）", en: "Managed by us (cloud + desktop client)" },
    deploy: { zh: "你的服务器 / 内网 GPU", en: "Your servers / on-prem GPUs" },
  },
  {
    label: { zh: "数据落点", en: "Where data lives" },
    coop: { zh: "托管实例，可签数据处理协议", en: "Managed instance, DPA available" },
    deploy: { zh: "全部留在你的内网，不出网", en: "Stays entirely inside your network" },
  },
  {
    label: { zh: "AI 模型与 Token", en: "AI models & tokens" },
    coop: { zh: "云端模型，按充值 Token 计费（年框协议价）", en: "Cloud models, token-metered at frame pricing" },
    deploy: { zh: "本地模型不限量；可选接云端 Key", en: "Unlimited local models; optional cloud keys" },
  },
  {
    label: { zh: "结算方式", en: "Billing" },
    coop: { zh: "月结 · 对公 · 发票", en: "Monthly · corporate · invoiced" },
    deploy: { zh: "一次性实施 + 年授权维保", en: "One-time setup + annual license & care" },
  },
  {
    label: { zh: "交付周期", en: "Time to live" },
    coop: { zh: "当天开通", en: "Same day" },
    deploy: { zh: "典型 1–2 周（勘察 → PoC → 部署 → 培训）", en: "Typically 1–2 weeks (survey → PoC → deploy → training)" },
  },
  {
    label: { zh: "适合谁", en: "Best for" },
    coop: { zh: "年用量 ≥10,000U 的成长团队", en: "Growing teams beyond ~10,000U a year" },
    deploy: { zh: "合规敏感 / 自有算力的组织", en: "Compliance-sensitive orgs / owned GPUs" },
  },
];

const STEPS: { icon: typeof ClipboardCheck; title: { zh: string; en: string }; desc: { zh: string; en: string } }[] = [
  {
    icon: ClipboardCheck,
    title: { zh: "① 需求勘察", en: "① Discovery" },
    desc: { zh: "1 个工作日内商务对接：平台矩阵、席位规模、合规边界、算力盘点。", en: "Sales responds within 1 business day: platforms, seats, compliance, hardware." },
  },
  {
    icon: Rocket,
    title: { zh: "② PoC 试点", en: "② PoC pilot" },
    desc: { zh: "3–5 天，用你的真实账号小规模跑：AI 人设承接 + 翻译 + 人工接管全链验证。", en: "3–5 days on your real accounts at small scale: personas, translation, human handoff." },
  },
  {
    icon: Server,
    title: { zh: "③ 部署实施", en: "③ Deployment" },
    desc: { zh: "年框=开通企业账号体系；私有化=引擎进内网 + 数据迁移 + 验收。", en: "Frame deal = org accounts provisioned; private = engine on-prem + migration + acceptance." },
  },
  {
    icon: GraduationCap,
    title: { zh: "④ 陪跑维保", en: "④ Care & growth" },
    desc: { zh: "坐席培训、SLA 支持、版本升级通道、季度用量与转化复盘。", en: "Agent training, SLA support, upgrade channel, quarterly usage & conversion reviews." },
  },
];

const SECURITY_POINTS: { icon: typeof Lock; zh: string; en: string }[] = [
  { icon: Database, zh: "私有化=数据不出网：会话、联系人、媒体全部落你的盘", en: "Private deploy keeps chats, contacts and media inside your network" },
  { icon: Lock, zh: "坐席分权与审计日志：谁看了什么、谁发了什么，可回放", en: "Role-based agents with replayable audit logs" },
  { icon: ShieldCheck, zh: "危机安全协议内置：识别→处置→资源保障全链有门禁测试", en: "Built-in crisis-safety protocol, gate-tested end to end" },
  { icon: Cable, zh: "多平台官方与 RPA 通道并存，账号资产可保全迁移", en: "Official APIs + RPA channels; account assets stay portable" },
];

// FAQ 单源在 lib/enterpriseContent.ts（页面渲染与路由 JSON-LD 共用）。
const FAQS = ENTERPRISE_FAQ;

/* ── 企业留资表单（打既有 /api/lead；interest 前缀 enterprise: 供 console 分流）── */

function EnterpriseLeadForm({ zh }: { zh: boolean }) {
  const { lang } = useLang();
  const [company, setCompany] = useState("");
  const [name, setName] = useState("");
  const [contact, setContact] = useState("");
  const [trackKey, setTrackKey] = useState("enterprise-coop");
  const [scale, setScale] = useState("6-20");
  const [message, setMessage] = useState("");
  const [hp, setHp] = useState("");
  const [status, setStatus] = useState<"idle" | "sending" | "ok" | "error">("idle");
  const [touched, setTouched] = useState(false);

  const contactOk = isValidContact(contact);
  const showErr = touched && contact.trim().length > 0 && !contactOk;

  const scaleOpts = ["1-5", "6-20", "21-100", "100+"];
  const trackOpts = [
    { v: "enterprise-coop", zh: "企业合作 · 年框（云端）", en: "Enterprise partnership (cloud frame deal)" },
    { v: "private-deploy", zh: "企业级私有化部署", en: "Private deployment" },
    { v: "unsure", zh: "还不确定，想先聊聊", en: "Not sure yet — let's talk" },
  ];

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (status === "sending") return;
    setTouched(true);
    if (!contactOk) return;
    setStatus("sending");
    try {
      const res = await fetch("/api/lead", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: name || company,
          contact,
          interest: `enterprise:${trackKey}`,
          message: `[公司] ${company || "-"} · [席位] ${scale}\n${message}`.slice(0, 1000),
          hp,
          lang,
          path: typeof window !== "undefined" ? window.location.pathname : "",
          source: "web",
          utm: getLeadUtm(),
        }),
      });
      if (!res.ok) throw new Error("bad");
      setStatus("ok");
      track("enterprise_lead_submit", { track: trackKey, scale });
    } catch {
      setStatus("error");
    }
  }

  const inputCls =
    "w-full rounded-xl border border-white/10 bg-ink-950/50 px-4 py-2.5 text-sm text-white placeholder:text-slate-500 outline-none transition focus:border-amber-300/50 focus:ring-1 focus:ring-amber-300/30";

  if (status === "ok") {
    return (
      <div className="mt-6 flex flex-col items-center gap-2 rounded-2xl border border-emerald-400/30 bg-emerald-400/10 px-6 py-10 text-center">
        <CheckCircle2 className="h-10 w-10 text-emerald-400" />
        <p className="text-lg font-semibold text-white">{zh ? "需求已收到" : "Request received"}</p>
        <p className="max-w-md text-sm text-slate-300">
          {zh
            ? "商务会在 1 个工作日内联系你。想更快？Telegram 直连通常几分钟就有人。"
            : "Sales will reach out within 1 business day. In a hurry? Telegram is usually minutes."}
        </p>
        <a
          href={CONTACT_URL}
          target="_blank"
          rel="noreferrer"
          onClick={() => track("enterprise_tg_cta", { from: "form_ok" })}
          className="mt-2 text-sm text-amber-300 hover:underline"
        >
          {zh ? "Telegram 直连商务 →" : "Open Telegram →"}
        </a>
      </div>
    );
  }

  return (
    <form onSubmit={onSubmit} className="mt-6 grid gap-4 md:grid-cols-2">
      <input type="text" tabIndex={-1} autoComplete="off" value={hp} onChange={(e) => setHp(e.target.value)} className="hidden" aria-hidden="true" />
      <div>
        <label className="mb-1.5 block text-xs text-slate-400">{zh ? "公司 / 团队" : "Company / team"}</label>
        <input className={inputCls} value={company} onChange={(e) => setCompany(e.target.value)} placeholder={zh ? "例：星河跨境" : "e.g. Acme Global"} maxLength={80} />
      </div>
      <div>
        <label className="mb-1.5 block text-xs text-slate-400">{zh ? "联系人" : "Your name"}</label>
        <input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} placeholder={zh ? "怎么称呼你" : "What should we call you"} maxLength={80} />
      </div>
      <div className="md:col-span-2">
        <label className="mb-1.5 block text-xs text-slate-400">
          {zh ? "联系方式（Telegram / 微信 / 邮箱 / 电话）" : "Contact (Telegram / WeChat / email / phone)"} <span className="text-amber-300">*</span>
        </label>
        <input
          className={`${inputCls} ${showErr ? "border-rose-400/60 focus:border-rose-400/60 focus:ring-rose-400/30" : ""}`}
          value={contact}
          onChange={(e) => setContact(e.target.value)}
          onBlur={() => setTouched(true)}
          placeholder={zh ? "@yourhandle / you@company.com / +86…" : "@yourhandle / you@company.com / +1…"}
          maxLength={200}
          required
          aria-invalid={showErr}
        />
        {showErr && <p className="mt-1.5 text-xs text-rose-400">{zh ? "看起来不像有效联系方式，请再检查一下" : "That doesn't look like a valid contact — please double-check"}</p>}
      </div>
      <div>
        <label className="mb-1.5 block text-xs text-slate-400">{zh ? "关注方向" : "Interested in"}</label>
        <select className={inputCls} value={trackKey} onChange={(e) => setTrackKey(e.target.value)}>
          {trackOpts.map((o) => (
            <option key={o.v} value={o.v} className="bg-ink-950 text-white">{zh ? o.zh : o.en}</option>
          ))}
        </select>
      </div>
      <div>
        <label className="mb-1.5 block text-xs text-slate-400">{zh ? "坐席规模" : "Seats"}</label>
        <select className={inputCls} value={scale} onChange={(e) => setScale(e.target.value)}>
          {scaleOpts.map((o) => (
            <option key={o} value={o} className="bg-ink-950 text-white">{o}</option>
          ))}
        </select>
      </div>
      <div className="md:col-span-2">
        <label className="mb-1.5 block text-xs text-slate-400">{zh ? "业务与需求（可选）" : "Your scenario (optional)"}</label>
        <textarea
          className={`${inputCls} min-h-[88px] resize-y`}
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          placeholder={zh ? "例：Telegram + WhatsApp 双平台，20 个坐席，客户以东南亚为主，考虑私有化…" : "e.g. Telegram + WhatsApp, 20 agents, SEA customers, considering private deployment…"}
          maxLength={800}
        />
      </div>
      <div className="md:col-span-2 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <button
          type="submit"
          disabled={status === "sending" || !contactOk}
          className="inline-flex items-center justify-center gap-2 rounded-full bg-gradient-to-r from-amber-300 to-amber-400 px-7 py-3 text-sm font-semibold text-ink-950 transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {status === "sending" ? (
            <>
              <Loader2 className="h-4 w-4 animate-spin" />
              {zh ? "提交中…" : "Sending…"}
            </>
          ) : (
            <>
              <Send className="h-4 w-4" />
              {zh ? "提交需求 · 1 个工作日内回复" : "Submit — reply within 1 business day"}
            </>
          )}
        </button>
        <span className="text-xs text-slate-500">{zh ? "仅用于商务联系，不会订阅任何邮件" : "Used only to reach you — no mailing lists"}</span>
      </div>
      {status === "error" && <p className="md:col-span-2 text-sm text-rose-400">{zh ? "提交失败，请稍后再试或走 Telegram 直连" : "Failed — retry later or use Telegram"}</p>}
    </form>
  );
}

/* ── 页面 ─────────────────────────────────────────────────────────────────── */

export default function EnterprisePage() {
  const { lang } = useLang();
  const zh = lang === "zh";

  useEffect(() => {
    track("enterprise_view", {});
  }, []);

  return (
    <section className="relative pb-24 pt-32">
      <div className="mx-auto max-w-6xl px-5">
        {/* Hero */}
        <Reveal eager>
          <div className="text-center">
            <span className="inline-flex items-center gap-1.5 rounded-full border border-amber-300/30 bg-amber-300/10 px-4 py-1.5 text-xs font-medium text-amber-300">
              <Building2 className="h-3.5 w-3.5" />
              {zh ? "企业服务 · 年框合作 / 私有化部署" : "Enterprise · frame deals / private deployment"}
            </span>
            <h1 className="mx-auto mt-5 max-w-3xl text-4xl font-bold leading-tight text-white md:text-5xl">
              {zh ? "把 AI 成交聊天，开进你的组织" : "Bring AI closing chat into your organization"}
            </h1>
            <p className="mx-auto mt-4 max-w-2xl text-sm leading-relaxed text-slate-400 md:text-base">
              {zh
                ? "多平台统一收件箱、AI 人设承接、免费标准翻译、人工接管——同一套引擎，两种企业形态：云端年框省心，私有化部署数据不出网。"
                : "Unified omni-channel inbox, AI personas, free standard translation, human handoff — one engine, two enterprise shapes: managed cloud frame deals, or private deployment that keeps data on-prem."}
            </p>
            <div className="mt-7 flex flex-wrap items-center justify-center gap-3">
              <a
                href="#lead"
                onClick={() => track("enterprise_cta", { to: "form" })}
                className="rounded-full bg-gradient-to-r from-amber-300 to-amber-400 px-8 py-3 text-sm font-semibold text-ink-950 shadow-[0_0_28px_rgba(252,211,77,0.25)] transition hover:opacity-90"
              >
                {zh ? "提交需求 · 预约演示" : "Submit request · book a demo"}
              </a>
              <a
                href={CONTACT_URL}
                target="_blank"
                rel="noreferrer"
                onClick={() => track("enterprise_tg_cta", { from: "hero" })}
                className="rounded-full border border-white/15 px-8 py-3 text-sm text-slate-200 transition hover:bg-white/5"
              >
                {zh ? "Telegram 直连商务" : "Talk on Telegram"}
              </a>
            </div>
          </div>
        </Reveal>

        {/* 两形态深卡（数据单源 ENTERPRISE_TRACKS + 页面叙事层） */}
        <Reveal className="mt-14">
          <div className="grid gap-5 md:grid-cols-2">
            {ENTERPRISE_TRACKS.map((t) => {
              const extra = TRACK_EXTRA[t.key];
              return (
                <div key={t.key} className="relative flex flex-col overflow-hidden rounded-3xl border border-amber-300/25 bg-gradient-to-br from-ink-900 to-[#1a1626] p-7">
                  <div className="pointer-events-none absolute -right-16 -top-20 h-56 w-56 rounded-full bg-amber-300/[0.07] blur-[80px]" />
                  {t.key === "enterprise-coop" ? <Building2 className="h-8 w-8 text-amber-300" /> : <Server className="h-8 w-8 text-amber-300" />}
                  <div className="mt-3 flex flex-wrap items-baseline gap-x-3">
                    <span className="text-lg font-semibold text-white">{zh ? t.name.zh : t.name.en}</span>
                    <span className="rounded-full bg-amber-300/15 px-2 py-0.5 text-[11px] text-amber-300">{zh ? "面议" : "Custom quote"}</span>
                  </div>
                  <p className="mt-1 text-sm text-slate-400">{zh ? t.tagline.zh : t.tagline.en}</p>
                  <p className="mt-3 text-xs leading-relaxed text-amber-200/80">{zh ? extra.fit.zh : extra.fit.en}</p>
                  <div className="mt-4 border-t border-white/5 pt-4">
                    <p className="text-[11px] font-medium uppercase tracking-wide text-slate-500">{zh ? "你会得到" : "What you get"}</p>
                    <ul className="mt-2 space-y-2">
                      {(zh ? extra.deliver.zh : extra.deliver.en).map((p) => (
                        <li key={p} className="flex items-start gap-2 text-xs leading-relaxed text-slate-300">
                          <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-300" />
                          {p}
                        </li>
                      ))}
                    </ul>
                  </div>
                  <a
                    href="#lead"
                    onClick={() => track("enterprise_cta", { to: "form", track: t.key })}
                    className="mt-6 rounded-full border border-amber-300/50 py-2.5 text-center text-sm text-amber-300 transition hover:bg-amber-300/10"
                  >
                    {zh ? "按这个方向聊 →" : "Start here →"}
                  </a>
                </div>
              );
            })}
          </div>
        </Reveal>

        {/* 对比表 */}
        <Reveal className="mt-14">
          <h2 className="text-center text-2xl font-bold text-white">{zh ? "两种形态，一张表看清" : "Two shapes, one table"}</h2>
          <div className="mt-6 overflow-x-auto rounded-2xl border border-white/10">
            <table className="w-full min-w-[640px] text-left text-sm">
              <thead>
                <tr className="border-b border-white/10 bg-white/[0.03] text-xs text-slate-400">
                  <th className="px-5 py-3 font-medium" />
                  <th className="px-5 py-3 font-medium text-amber-300">{zh ? "企业合作 · 年框" : "Frame deal"}</th>
                  <th className="px-5 py-3 font-medium text-amber-300">{zh ? "私有化部署" : "Private deployment"}</th>
                </tr>
              </thead>
              <tbody>
                {COMPARE_ROWS.map((r) => (
                  <tr key={r.label.zh} className="border-b border-white/5 last:border-0">
                    <td className="px-5 py-3.5 text-xs text-slate-500">{zh ? r.label.zh : r.label.en}</td>
                    <td className="px-5 py-3.5 text-xs leading-relaxed text-slate-300">{zh ? r.coop.zh : r.coop.en}</td>
                    <td className="px-5 py-3.5 text-xs leading-relaxed text-slate-300">{zh ? r.deploy.zh : r.deploy.en}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Reveal>

        {/* 实施流程 */}
        <Reveal className="mt-14">
          <h2 className="text-center text-2xl font-bold text-white">{zh ? "从对接到上线" : "From first call to go-live"}</h2>
          <div className="mt-6 grid gap-4 md:grid-cols-4">
            {STEPS.map((s) => (
              <div key={s.title.zh} className="rounded-2xl border border-white/10 bg-ink-900/50 p-5">
                <s.icon className="h-6 w-6 text-amber-300" />
                <p className="mt-3 text-sm font-semibold text-white">{zh ? s.title.zh : s.title.en}</p>
                <p className="mt-1.5 text-xs leading-relaxed text-slate-400">{zh ? s.desc.zh : s.desc.en}</p>
              </div>
            ))}
          </div>
        </Reveal>

        {/* 安全与合规 */}
        <Reveal className="mt-14">
          <div className="rounded-3xl border border-white/10 bg-ink-900/50 p-7 md:p-9">
            <div className="flex flex-wrap items-center gap-3">
              <ShieldCheck className="h-7 w-7 text-emerald-400" />
              <h2 className="text-xl font-bold text-white">{zh ? "安全与合规，是默认配置" : "Security & compliance by default"}</h2>
              <Link
                href={zh ? "/compliance" : "/en/compliance"}
                className="ml-auto inline-flex items-center gap-1 text-xs text-slate-400 transition hover:text-white"
              >
                {zh ? "查看合规能力页" : "See compliance page"}
                <ArrowRight className="h-3.5 w-3.5" />
              </Link>
            </div>
            <div className="mt-5 grid gap-4 md:grid-cols-2">
              {SECURITY_POINTS.map((p) => (
                <div key={p.zh} className="flex items-start gap-3">
                  <p.icon className="mt-0.5 h-5 w-5 shrink-0 text-emerald-400/80" />
                  <p className="text-sm leading-relaxed text-slate-300">{zh ? p.zh : p.en}</p>
                </div>
              ))}
            </div>
          </div>
        </Reveal>

        {/* FAQ */}
        <Reveal className="mt-14">
          <h2 className="text-center text-2xl font-bold text-white">{zh ? "企业客户常问" : "Enterprise FAQ"}</h2>
          <div className="mx-auto mt-6 max-w-3xl space-y-3">
            {FAQS.map((f) => (
              <details key={f.q.zh} className="group rounded-2xl border border-white/10 bg-ink-900/50 px-5 py-4">
                <summary className="cursor-pointer list-none text-sm font-medium text-white transition group-open:text-amber-300">
                  {zh ? f.q.zh : f.q.en}
                </summary>
                <p className="mt-2 text-sm leading-relaxed text-slate-400">{zh ? f.a.zh : f.a.en}</p>
              </details>
            ))}
          </div>
        </Reveal>

        {/* 留资表单 */}
        <Reveal className="mt-16">
          <div id="lead" className="scroll-mt-28 rounded-3xl border border-amber-300/25 bg-gradient-to-br from-[#241a33] via-ink-900 to-[#12233a] p-7 md:p-10">
            <div className="flex flex-wrap items-center gap-3">
              <Sparkles className="h-6 w-6 text-amber-300" />
              <h2 className="text-xl font-bold text-white">{zh ? "告诉我们你的场景" : "Tell us about your scenario"}</h2>
            </div>
            <p className="mt-2 max-w-2xl text-sm text-slate-400">
              {zh
                ? "留下联系方式与大致规模，商务在 1 个工作日内回复；也可以 Telegram 直连，通常几分钟就有人。"
                : "Leave a contact and rough scale — sales replies within 1 business day. Telegram is usually minutes."}
            </p>
            <EnterpriseLeadForm zh={zh} />
          </div>
        </Reveal>
      </div>
    </section>
  );
}
