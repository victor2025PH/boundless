// /console 服务端共享展示件：徽章、卡片、表格、空态、分页、格式化。
// 无 "use client" —— 全部可在服务端组件里直接使用。
// 配色纪律（2026-08 视觉批，BRAND_TOKENS §6 判定沉淀的 console 版）：
//   · 暗底一律 ink 深空阶（slate 禁作暗底）；文字冷灰仍可用 slate 文字阶；
//   · crown-* = 控制台强调色（链接/筛选钮/标题竖条等「独立作强调」的位置）；
//   · 各状态徽章里的 amber 臂（订单 pending / 到期高亮 / master 角色…）是
//     多色家族的一员，按判定保留 Tailwind 字面量，勿改成 crown。

import Link from "next/link";
import type { ReactNode } from "react";
import { BookOpen, FlaskConical, MessageSquareText, Mic, ScanFace } from "lucide-react";
import {
  CHANNEL_PLATFORM_LABEL,
  CHANNEL_STATUS_LABEL,
  LEAD_STATUS_LABEL,
  LICENSE_STATUS_LABEL,
  OPP_KIND_LABEL,
  ORDER_STATUS_LABEL,
  PERSONA_SLOT_DESC,
  PERSONA_SLOT_LABEL,
  PERSONA_STATUS_LABEL,
  ROLE_LABEL,
  SYSTEM_LABEL,
  fingerprintLabel,
  lbl,
  licenseQuotaLabel,
} from "./labels";

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

/** ISO 时间 → 相对时间（「3 分钟前」），title 放绝对时间。 */
export function fmtRelative(iso: string | null | undefined): { text: string; title?: string } {
  if (!iso) return { text: "—" };
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return { text: iso };
  const abs = fmtDateTime(iso);
  const diffMin = Math.round((Date.now() - t) / 60_000);
  if (Math.abs(diffMin) < 1) return { text: "刚刚", title: abs };
  if (diffMin >= 0 && diffMin < 60) return { text: `${diffMin} 分钟前`, title: abs };
  if (diffMin >= 0 && diffMin < 24 * 60) return { text: `${Math.floor(diffMin / 60)} 小时前`, title: abs };
  if (diffMin >= 0 && diffMin < 7 * 24 * 60) {
    const days = Math.floor(diffMin / (24 * 60));
    return { text: days === 1 ? `昨天 ${abs.slice(11)}` : `${days} 天前`, title: abs };
  }
  return { text: abs };
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
const BADGE = "inline-flex h-5 items-center rounded-full border px-2 text-[11px] font-medium";
const ORDER_STATUS_STYLE: Record<string, string> = {
  pending: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  paid: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  activated: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  cancelled: "bg-slate-500/15 text-slate-400 border-slate-500/30",
  refunded: "bg-slate-500/15 text-slate-400 border-slate-500/30",
};

export function OrderStatusBadge({ status }: { status: string | null }) {
  const s = status ?? "";
  const cls = ORDER_STATUS_STYLE[s] ?? "bg-slate-500/15 text-slate-400 border-slate-500/30";
  return <span className={`${BADGE} ${cls}`}>{lbl(ORDER_STATUS_LABEL, s || null)}</span>;
}

const LEAD_STATUS_STYLE: Record<string, string> = {
  new: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  contacted: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  won: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  lost: "bg-slate-500/15 text-slate-400 border-slate-500/30",
};

export function LeadStatusBadge({ status }: { status: string | null }) {
  const s = status ?? "";
  const cls = LEAD_STATUS_STYLE[s] ?? "bg-slate-500/15 text-slate-400 border-slate-500/30";
  return <span className={`${BADGE} ${cls}`}>{lbl(LEAD_STATUS_LABEL, s || null)}</span>;
}

const ROLE_STYLE: Record<string, string> = {
  master: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  admin: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  viewer: "bg-slate-500/15 text-slate-300 border-slate-500/30",
};

/** 控制台角色徽章（页头 / 用户列表共用）。中文来自 labels.ts，原枚举进 title。 */
export function RoleBadge({ role }: { role: string; compact?: boolean }) {
  const cls = ROLE_STYLE[role] ?? ROLE_STYLE.viewer;
  return (
    <span title={role} className={`${BADGE} ${cls}`}>
      {lbl(ROLE_LABEL, role)}
    </span>
  );
}

const SYSTEM_STYLE: Record<string, string> = {
  avatarhub: "bg-violet-500/15 text-violet-300 border-violet-500/30",
  chengjie: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  huoke: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  tgkz2026: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  website: "bg-cyan-500/15 text-cyan-300 border-cyan-500/30",
};

export function SystemBadge({ system }: { system: string }) {
  const cls = SYSTEM_STYLE[system] ?? "bg-slate-500/15 text-slate-400 border-slate-500/30";
  return (
    <span title={system} className={`${BADGE} ${cls}`}>
      {lbl(SYSTEM_LABEL, system)}
    </span>
  );
}

const LICENSE_STATUS_STYLE: Record<string, string> = {
  active: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  trial: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  expired: "bg-rose-500/15 text-rose-300 border-rose-500/30",
  revoked: "bg-rose-500/15 text-rose-300 border-rose-500/30",
  unknown: "bg-slate-500/15 text-slate-400 border-slate-500/30",
};

export function LicenseStatusBadge({ status }: { status: string | null }) {
  const s = status ?? "";
  const cls = LICENSE_STATUS_STYLE[s] ?? "bg-slate-500/15 text-slate-400 border-slate-500/30";
  return (
    <span title={s || undefined} className={`${BADGE} ${cls}`}>
      {lbl(LICENSE_STATUS_LABEL, s || null)}
    </span>
  );
}

export function FingerprintCell({ fingerprint }: { fingerprint: string | null | undefined }) {
  const { text, title } = fingerprintLabel(fingerprint);
  return (
    <span title={title} className="text-xs text-slate-400">
      {text}
    </span>
  );
}

export function QuotaCell({ raw }: { raw: string | null | undefined }) {
  const { text, title } = licenseQuotaLabel(raw);
  return (
    <span title={title} className="text-xs tabular-nums text-slate-300">
      {text}
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
export function ChannelPlatformBadge({ platform }: { platform: string }) {
  const cls = CHANNEL_PLATFORM_STYLE[platform] ?? CHANNEL_PLATFORM_STYLE.other;
  return (
    <span title={platform} className={`${BADGE} ${cls}`}>
      {lbl(CHANNEL_PLATFORM_LABEL, platform)}
    </span>
  );
}

const CHANNEL_STATUS_STYLE: Record<string, string> = {
  active: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  pending: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  paused: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  revoked: "bg-rose-500/15 text-rose-300 border-rose-500/30",
};
export function ChannelStatusBadge({ status }: { status: string | null }) {
  const s = status ?? "";
  const cls = CHANNEL_STATUS_STYLE[s] ?? "bg-slate-500/15 text-slate-400 border-slate-500/30";
  return <span className={`${BADGE} ${cls}`}>{lbl(CHANNEL_STATUS_LABEL, s || null)}</span>;
}

// ── 人设总线（schema v3）───────────────────────────────────────────
const PERSONA_STATUS_STYLE: Record<string, string> = {
  active: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  archived: "bg-slate-500/15 text-slate-400 border-slate-500/30",
  purge_pending: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  purged: "bg-rose-500/15 text-rose-300 border-rose-500/30",
};
export function PersonaStatusBadge({ status }: { status: string | null }) {
  const s = status ?? "";
  const cls = PERSONA_STATUS_STYLE[s] ?? "bg-slate-500/15 text-slate-400 border-slate-500/30";
  return <span className={`${BADGE} ${cls}`}>{lbl(PERSONA_STATUS_LABEL, s || null)}</span>;
}

export const PERSONA_SLOT_META = [
  { key: "face", label: PERSONA_SLOT_LABEL.face, title: PERSONA_SLOT_DESC.face, Icon: ScanFace, lit: "border-violet-500/40 bg-violet-500/15 text-violet-300" },
  { key: "voice", label: PERSONA_SLOT_LABEL.voice, title: PERSONA_SLOT_DESC.voice, Icon: Mic, lit: "border-sky-500/40 bg-sky-500/15 text-sky-300" },
  { key: "prompt", label: PERSONA_SLOT_LABEL.prompt, title: PERSONA_SLOT_DESC.prompt, Icon: MessageSquareText, lit: "border-amber-500/40 bg-amber-500/15 text-amber-300" },
  { key: "knowledge", label: PERSONA_SLOT_LABEL.knowledge, title: PERSONA_SLOT_DESC.knowledge, Icon: BookOpen, lit: "border-emerald-500/40 bg-emerald-500/15 text-emerald-300" },
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
      {PERSONA_SLOT_META.map(({ key, label, title, Icon, lit: litCls }) => (
        <span
          key={key}
          title={`${title || label}：${lit[key] ? "已配置" : "未配置"}`}
          className={`inline-flex h-6 w-6 items-center justify-center rounded-md border ${
            lit[key] ? litCls : "border-ink-700 bg-ink-900/60 text-slate-700"
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
/** 商机类型徽章（总览商机卡 / 客户 360 商机分区共用）。 */
export function OpportunityKindBadge({ kind }: { kind: string }) {
  const cls = OPPORTUNITY_KIND_STYLE[kind] ?? "bg-slate-500/15 text-slate-400 border-slate-500/30";
  return (
    <span title={kind} className={`inline-flex h-5 shrink-0 items-center rounded-full border px-2 text-[11px] font-medium ${cls}`}>
      {lbl(OPP_KIND_LABEL, kind)}
    </span>
  );
}

// ── 测试/演练数据（schema v6 is_test）───────────────────────────────
/** 测试数据徽章：标记 is_test=1 的行。灰/石板低调配色，不与业务状态徽章抢视觉。 */
export function TestBadge({ className = "" }: { className?: string }) {
  return (
    <span
      title="测试/演练数据，不计入 KPI 与商机"
      className={`inline-flex shrink-0 items-center gap-1 rounded-full border border-slate-500/30 bg-slate-500/15 px-2 py-0.5 text-[11px] font-medium text-slate-400 ${className}`}
    >
      <FlaskConical className="h-3 w-3" />
      测试
    </span>
  );
}

/** 「显示测试数据」开关：链接在 ?test=1 与去掉之间切换（保留其余筛选参数，切换即回第一页）。
 *  testCount 为当前筛选条件下的测试数据条数；没有测试数据且未开启时不渲染。 */
export function TestFilterToggle({
  basePath,
  params,
  showTest,
  testCount,
  className = "",
}: {
  basePath: string;
  params: Record<string, string | undefined>;
  showTest: boolean;
  testCount: number;
  className?: string;
}) {
  if (!showTest && testCount <= 0) return null;
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v) sp.set(k, v);
  if (!showTest) sp.set("test", "1");
  const qs = sp.toString();
  return (
    <Link
      href={qs ? `${basePath}?${qs}` : basePath}
      title="测试/演练数据（e2e / smoke 等）不计入 KPI 与商机；此开关只影响本列表的展示"
      className={`inline-flex items-center gap-1 text-xs underline-offset-2 hover:underline ${
        showTest ? "text-crown-300" : "text-slate-500 hover:text-slate-300"
      } ${className}`}
    >
      <FlaskConical className="h-3.5 w-3.5" />
      {showTest ? `隐藏测试数据（${testCount} 条）` : `显示测试数据（${testCount} 条）`}
    </Link>
  );
}

// ── 布局件 ──────────────────────────────────────────────────────────
export function Card({ children, className = "", id }: { children: ReactNode; className?: string; id?: string }) {
  return (
    <div id={id} className={`rounded-2xl border border-ink-700 bg-ink-900/60 p-5 ${className}`}>{children}</div>
  );
}

export function SectionTitle({ children, count }: { children: ReactNode; count?: number }) {
  return (
    <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold text-white">
      <span className="inline-block h-3.5 w-1 rounded-full bg-crown-400" />
      {children}
      {count !== undefined && (
        <span className="rounded-full bg-ink-700 px-2 py-0.5 text-[11px] font-medium text-slate-400">{count}</span>
      )}
    </h2>
  );
}

export function PageHeader({
  title,
  desc,
  actions,
  techNote,
}: {
  title: string;
  desc?: ReactNode;
  actions?: ReactNode;
  techNote?: ReactNode;
}) {
  return (
    <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <h1 className="text-xl font-semibold text-white">{title}</h1>
        {desc && <p className="mt-1 max-w-2xl text-xs leading-relaxed text-slate-400">{desc}</p>}
        {techNote && (
          <details className="mt-2 max-w-2xl text-xs text-slate-500">
            <summary className="cursor-pointer select-none text-slate-400 hover:text-slate-300">技术说明</summary>
            <div className="mt-1.5 leading-relaxed">{techNote}</div>
          </details>
        )}
      </div>
      {actions}
    </div>
  );
}

/** 表格骨架：th 列表 + tbody 内容。 */
export function DataTable({ head, children }: { head: string[]; children: ReactNode }) {
  return (
    <div className="overflow-x-auto rounded-xl border border-ink-700">
      <table className="w-full min-w-max text-left text-sm">
        <thead>
          <tr className="border-b border-ink-700 bg-ink-900/80 text-xs font-medium text-slate-400">
            {head.map((h) => (
              <th key={h} className="px-3 py-2.5 font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-ink-700/70 text-[13px] [&>tr:nth-child(even)]:bg-ink-900/30 [&>tr:hover]:bg-ink-800/60">{children}</tbody>
      </table>
    </div>
  );
}

export function Td({ children, className = "", title }: { children: ReactNode; className?: string; title?: string }) {
  return (
    <td title={title} className={`px-3 py-2.5 align-middle ${className}`}>
      {children}
    </td>
  );
}

/** 空库/空结果引导。 */
export function EmptyState({ title, hints }: { title: string; hints: ReactNode[] }) {
  return (
    <div className="rounded-xl border border-dashed border-slate-700 bg-ink-900/40 px-6 py-10 text-center">
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
  return <code className="rounded bg-ink-700 px-1.5 py-0.5 font-mono text-[11px] text-crown-300/90">{children}</code>;
}

/** 客户列：已归属 → 链到客户 360；未归属由调用方渲染归属控件。 */
export function CustomerLink({ customerId, label }: { customerId: string; label?: string | null }) {
  return (
    <Link
      href={`/console/customers/${customerId}`}
      className="text-xs font-medium text-crown-300 underline-offset-2 hover:underline"
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
  const linkCls = "rounded-lg border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:border-crown-500/60 hover:text-crown-300";
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
  "rounded-lg border border-slate-700 bg-ink-950 px-3 py-1.5 text-xs text-slate-200 outline-none placeholder:text-slate-500 focus:border-crown-500";

export function FilterSubmit() {
  return (
    <button
      type="submit"
      className="rounded-lg border border-crown-500/40 px-3 py-1.5 text-xs font-medium text-crown-300 hover:bg-crown-500/10"
    >
      筛选
    </button>
  );
}
