// /console 服务端共享展示件：徽章、卡片、表格、空态、分页、格式化。
// 无 "use client" —— 全部可在服务端组件里直接使用。

import Link from "next/link";
import type { ReactNode } from "react";
import { BookOpen, MessageSquareText, Mic, ScanFace } from "lucide-react";

// ── 格式化 ──────────────────────────────────────────────────────────
const TZ_OFFSET_H = Number(process.env.TZ_OFFSET ?? 8); // 与 /admin 同源约定：站点时区默认 UTC+8

/** ISO 时间 → "YYYY-MM-DD HH:mm"（站点时区）；空值返回 "—"。 */
export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return iso;
  const d = new Date(t + TZ_OFFSET_H * 3600_000);
  return d.toISOString().slice(0, 16).replace("T", " ");
}

/** ISO 时间 → "YYYY-MM-DD"（站点时区）。 */
export function fmtDate(iso: string | null | undefined): string {
  return fmtDateTime(iso).slice(0, 10);
}

/** 距到期天数（向上取整）；无值/不可解析返回 null。 */
export function daysUntil(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return null;
  return Math.ceil((t - Date.now()) / 86400_000);
}

/** 金额 + 币种；pay_amount 与 amount 不同时以「实付(原价)」呈现。 */
export function fmtAmount(
  amount: number | null | undefined,
  payAmount: number | null | undefined,
  currency: string | null | undefined
): string {
  const cur = currency ?? "";
  const f = (n: number) => `${n} ${cur}`.trim();
  if (payAmount != null && amount != null && payAmount !== amount) return `${f(payAmount)}（原 ${f(amount)}）`;
  const n = payAmount ?? amount;
  return n == null ? "—" : f(n);
}

/** 长 ID 缩短显示（保留前缀 + 末 4 位），title 提供完整值。 */
export function ShortId({ id }: { id: string }) {
  const short = id.length > 14 ? `${id.slice(0, 5)}…${id.slice(-4)}` : id;
  return (
    <span title={id} className="font-mono text-xs text-slate-400">
      {short}
    </span>
  );
}

// ── 徽章 ────────────────────────────────────────────────────────────
const ORDER_STATUS_STYLE: Record<string, string> = {
  pending: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  paid: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  activated: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  cancelled: "bg-slate-500/15 text-slate-400 border-slate-500/30",
};
const ORDER_STATUS_LABEL: Record<string, string> = {
  pending: "待支付",
  paid: "已支付",
  activated: "已开通",
  cancelled: "已取消",
};

export function OrderStatusBadge({ status }: { status: string | null }) {
  const s = status ?? "(空)";
  const cls = ORDER_STATUS_STYLE[s] ?? "bg-slate-500/15 text-slate-400 border-slate-500/30";
  return (
    <span className={`inline-block rounded-full border px-2 py-0.5 text-[11px] font-medium ${cls}`}>
      {ORDER_STATUS_LABEL[s] ?? s}
    </span>
  );
}

const LEAD_STATUS_STYLE: Record<string, string> = {
  new: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  contacted: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  won: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  lost: "bg-slate-500/15 text-slate-400 border-slate-500/30",
};
const LEAD_STATUS_LABEL: Record<string, string> = {
  new: "新留资",
  contacted: "已联系",
  won: "已成交",
  lost: "已流失",
};

export function LeadStatusBadge({ status }: { status: string | null }) {
  const s = status ?? "(空)";
  const cls = LEAD_STATUS_STYLE[s] ?? "bg-slate-500/15 text-slate-400 border-slate-500/30";
  return (
    <span className={`inline-block rounded-full border px-2 py-0.5 text-[11px] font-medium ${cls}`}>
      {LEAD_STATUS_LABEL[s] ?? s}
    </span>
  );
}

const ROLE_STYLE: Record<string, string> = {
  master: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  admin: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  viewer: "bg-slate-500/15 text-slate-300 border-slate-500/30",
};
const ROLE_LABEL: Record<string, string> = {
  master: "master 主账号",
  admin: "admin 运营",
  viewer: "viewer 只读",
};

/** 控制台角色徽章（页头 / 用户列表共用）。compact=true 只显示角色名。 */
export function RoleBadge({ role, compact = false }: { role: string; compact?: boolean }) {
  const cls = ROLE_STYLE[role] ?? ROLE_STYLE.viewer;
  return (
    <span className={`inline-block rounded-full border px-2 py-0.5 font-mono text-[11px] font-medium ${cls}`}>
      {compact ? role : ROLE_LABEL[role] ?? role}
    </span>
  );
}

const SYSTEM_STYLE: Record<string, string> = {
  avatarhub: "bg-violet-500/15 text-violet-300 border-violet-500/30",
  chengjie: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  huoke: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
};

export function SystemBadge({ system }: { system: string }) {
  const cls = SYSTEM_STYLE[system] ?? "bg-slate-500/15 text-slate-400 border-slate-500/30";
  return (
    <span className={`inline-block rounded-full border px-2 py-0.5 font-mono text-[11px] font-medium ${cls}`}>
      {system}
    </span>
  );
}

// ── 渠道账号台账（schema v5）────────────────────────────────────────
const CHANNEL_PLATFORM_STYLE: Record<string, string> = {
  telegram: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  whatsapp: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  messenger: "bg-violet-500/15 text-violet-300 border-violet-500/30",
  line: "bg-lime-500/15 text-lime-300 border-lime-500/30",
  web: "bg-cyan-500/15 text-cyan-300 border-cyan-500/30",
  other: "bg-slate-500/15 text-slate-400 border-slate-500/30",
};
const CHANNEL_PLATFORM_LABEL: Record<string, string> = {
  telegram: "Telegram",
  whatsapp: "WhatsApp",
  messenger: "Messenger",
  line: "LINE",
  web: "web 客服",
  other: "其他",
};

export function ChannelPlatformBadge({ platform }: { platform: string }) {
  const cls = CHANNEL_PLATFORM_STYLE[platform] ?? CHANNEL_PLATFORM_STYLE.other;
  return (
    <span className={`inline-block rounded-full border px-2 py-0.5 text-[11px] font-medium ${cls}`}>
      {CHANNEL_PLATFORM_LABEL[platform] ?? platform}
    </span>
  );
}

const CHANNEL_STATUS_STYLE: Record<string, string> = {
  active: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  pending: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  paused: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  revoked: "bg-rose-500/15 text-rose-300 border-rose-500/30",
};
const CHANNEL_STATUS_LABEL: Record<string, string> = {
  active: "在用",
  pending: "待启用",
  paused: "已暂停",
  revoked: "已弃用",
};

export function ChannelStatusBadge({ status }: { status: string | null }) {
  const s = status ?? "(空)";
  const cls = CHANNEL_STATUS_STYLE[s] ?? "bg-slate-500/15 text-slate-400 border-slate-500/30";
  return (
    <span className={`inline-block rounded-full border px-2 py-0.5 text-[11px] font-medium ${cls}`}>
      {CHANNEL_STATUS_LABEL[s] ?? s}
    </span>
  );
}

// ── 人设总线（schema v3）───────────────────────────────────────────
const PERSONA_STATUS_STYLE: Record<string, string> = {
  active: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  archived: "bg-slate-500/15 text-slate-400 border-slate-500/30",
  purge_pending: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  purged: "bg-rose-500/15 text-rose-300 border-rose-500/30",
};
const PERSONA_STATUS_LABEL: Record<string, string> = {
  active: "在用",
  archived: "已归档",
  purge_pending: "清除中",
  purged: "已清除",
};

export function PersonaStatusBadge({ status }: { status: string | null }) {
  const s = status ?? "(空)";
  const cls = PERSONA_STATUS_STYLE[s] ?? "bg-slate-500/15 text-slate-400 border-slate-500/30";
  return (
    <span className={`inline-block rounded-full border px-2 py-0.5 text-[11px] font-medium ${cls}`}>
      {PERSONA_STATUS_LABEL[s] ?? s}
    </span>
  );
}

export const PERSONA_SLOT_META = [
  { key: "face", label: "face 形象/脸模", Icon: ScanFace, lit: "border-violet-500/40 bg-violet-500/15 text-violet-300" },
  { key: "voice", label: "voice 声纹克隆", Icon: Mic, lit: "border-sky-500/40 bg-sky-500/15 text-sky-300" },
  { key: "prompt", label: "prompt 语言人格/话术", Icon: MessageSquareText, lit: "border-amber-500/40 bg-amber-500/15 text-amber-300" },
  { key: "knowledge", label: "knowledge 术语库/知识库", Icon: BookOpen, lit: "border-emerald-500/40 bg-emerald-500/15 text-emerald-300" },
] as const;

/** 人设四槽位图示：face/voice/prompt/knowledge 点亮态（列表/详情共用）。 */
export function PersonaSlotCells({
  face,
  voice,
  prompt,
  knowledge,
}: {
  face: boolean;
  voice: boolean;
  prompt: boolean;
  knowledge: boolean;
}) {
  const lit: Record<string, boolean> = { face, voice, prompt, knowledge };
  return (
    <span className="inline-flex gap-1">
      {PERSONA_SLOT_META.map(({ key, label, Icon, lit: litCls }) => (
        <span
          key={key}
          title={`${label}：${lit[key] ? "已配置" : "未配置"}`}
          className={`inline-flex h-6 w-6 items-center justify-center rounded-md border ${
            lit[key] ? litCls : "border-slate-800 bg-slate-900/60 text-slate-700"
          }`}
        >
          <Icon className="h-3.5 w-3.5" />
        </span>
      ))}
    </span>
  );
}

/** 授权状态 + 到期高亮：30 天内到期 → 琥珀，已过期/吊销 → 玫红。 */
export function ExpiryCell({ expiresAt }: { expiresAt: string | null }) {
  const days = daysUntil(expiresAt);
  if (expiresAt == null) return <span className="text-slate-500">—</span>;
  if (days == null) return <span className="text-slate-400">{expiresAt}</span>;
  if (days <= 0) {
    return (
      <span className="font-medium text-rose-400">
        {fmtDate(expiresAt)} · 已过期
      </span>
    );
  }
  if (days <= 30) {
    return (
      <span className="rounded-md bg-amber-500/15 px-1.5 py-0.5 font-medium text-amber-300">
        {fmtDate(expiresAt)} · 剩 {days} 天
      </span>
    );
  }
  return <span className="text-slate-300">{fmtDate(expiresAt)}</span>;
}

// ── 跨售商机（lib/opportunities.ts 三类规则）────────────────────────
const OPPORTUNITY_KIND_STYLE: Record<string, string> = {
  persona_cross_sell: "bg-violet-500/15 text-violet-300 border-violet-500/30",
  product_gap_cross_sell: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  expiring_renewal: "bg-amber-500/15 text-amber-300 border-amber-500/30",
};
const OPPORTUNITY_KIND_LABEL: Record<string, string> = {
  persona_cross_sell: "人设跨售",
  product_gap_cross_sell: "互补缺口",
  expiring_renewal: "续费在即",
};

/** 商机类型徽章（总览商机卡 / 客户 360 商机分区共用）。 */
export function OpportunityKindBadge({ kind }: { kind: string }) {
  const cls = OPPORTUNITY_KIND_STYLE[kind] ?? "bg-slate-500/15 text-slate-400 border-slate-500/30";
  return (
    <span className={`inline-block shrink-0 rounded-full border px-2 py-0.5 text-[11px] font-medium ${cls}`}>
      {OPPORTUNITY_KIND_LABEL[kind] ?? kind}
    </span>
  );
}

// ── 布局件 ──────────────────────────────────────────────────────────
export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <div className={`rounded-2xl border border-slate-800 bg-slate-900/60 p-5 ${className}`}>{children}</div>
  );
}

export function SectionTitle({ children, count }: { children: ReactNode; count?: number }) {
  return (
    <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold text-white">
      <span className="inline-block h-3.5 w-1 rounded-full bg-amber-400" />
      {children}
      {count !== undefined && (
        <span className="rounded-full bg-slate-800 px-2 py-0.5 text-[11px] font-medium text-slate-400">{count}</span>
      )}
    </h2>
  );
}

export function PageHeader({ title, desc, actions }: { title: string; desc?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 className="text-lg font-bold text-white">{title}</h1>
        {desc && <p className="mt-1 max-w-2xl text-xs leading-relaxed text-slate-500">{desc}</p>}
      </div>
      {actions}
    </div>
  );
}

/** 表格骨架：th 列表 + tbody 内容。 */
export function DataTable({ head, children }: { head: string[]; children: ReactNode }) {
  return (
    <div className="overflow-x-auto rounded-xl border border-slate-800">
      <table className="w-full min-w-max text-left text-sm">
        <thead>
          <tr className="border-b border-slate-800 bg-slate-900/80 text-[11px] uppercase tracking-wider text-slate-500">
            {head.map((h) => (
              <th key={h} className="px-3 py-2.5 font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-800/70">{children}</tbody>
      </table>
    </div>
  );
}

export function Td({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <td className={`px-3 py-2.5 align-middle ${className}`}>{children}</td>;
}

/** 空库/空结果引导。 */
export function EmptyState({ title, hints }: { title: string; hints: ReactNode[] }) {
  return (
    <div className="rounded-xl border border-dashed border-slate-700 bg-slate-900/40 px-6 py-10 text-center">
      <p className="text-sm font-medium text-slate-300">{title}</p>
      <ul className="mx-auto mt-3 max-w-xl space-y-1.5 text-xs leading-relaxed text-slate-500">
        {hints.map((h, i) => (
          <li key={i}>{h}</li>
        ))}
      </ul>
    </div>
  );
}

export function Code({ children }: { children: ReactNode }) {
  return <code className="rounded bg-slate-800 px-1.5 py-0.5 font-mono text-[11px] text-amber-300/90">{children}</code>;
}

/** 客户列：已归属 → 链到客户 360；未归属由调用方渲染归属控件。 */
export function CustomerLink({ customerId, label }: { customerId: string; label?: string | null }) {
  return (
    <Link
      href={`/console/customers/${customerId}`}
      className="text-xs font-medium text-amber-300 underline-offset-2 hover:underline"
      title={customerId}
    >
      {label || `${customerId.slice(0, 5)}…${customerId.slice(-4)}`}
    </Link>
  );
}

/** 分页（保留其余查询参数）。 */
export function Pager({
  basePath,
  params,
  total,
  limit,
  offset,
}: {
  basePath: string;
  params: Record<string, string | undefined>;
  total: number;
  limit: number;
  offset: number;
}) {
  if (total <= limit) return null;
  const mk = (off: number) => {
    const sp = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v) sp.set(k, v);
    if (off > 0) sp.set("offset", String(off));
    const qs = sp.toString();
    return qs ? `${basePath}?${qs}` : basePath;
  };
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.ceil(total / limit);
  const linkCls = "rounded-lg border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:border-amber-500/60 hover:text-amber-300";
  return (
    <div className="mt-4 flex items-center justify-between text-xs text-slate-500">
      <span>
        共 {total} 条 · 第 {page}/{pages} 页
      </span>
      <div className="flex gap-2">
        {offset > 0 && (
          <Link href={mk(Math.max(0, offset - limit))} className={linkCls}>
            ← 上一页
          </Link>
        )}
        {offset + limit < total && (
          <Link href={mk(offset + limit)} className={linkCls}>
            下一页 →
          </Link>
        )}
      </div>
    </div>
  );
}

// ── 查询表单（纯 GET 表单，无需客户端 JS）───────────────────────────
export const filterInputCls =
  "rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-xs text-slate-200 outline-none placeholder:text-slate-600 focus:border-amber-500";

export function FilterSubmit() {
  return (
    <button
      type="submit"
      className="rounded-lg border border-amber-500/40 px-3 py-1.5 text-xs font-medium text-amber-300 hover:bg-amber-500/10"
    >
      筛选
    </button>
  );
}
