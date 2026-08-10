"use client";

// /console 客户端交互组件集：登录卡（账号 + 初始化引导）、导航高亮、toast、
// 新建客户、归属客户、挂身份。服务端页面只读账本直出，所有写操作经这里
// fetch /api/console/**，成功后 router.refresh()。

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  BarChart3,
  Bug,
  Filter,
  Gift,
  Inbox,
  KeyRound,
  LayoutDashboard,
  Link2,
  Lock,
  LogOut,
  Plus,
  Radio,
  ReceiptText,
  ScrollText,
  ShieldAlert,
  Sparkles,
  UserCog,
  UserRound,
  Users,
  VenetianMask,
  X,
} from "lucide-react";

// ── toast（模块级事件总线，ConsoleToaster 挂在 layout）─────────────
type ToastMsg = { msg: string; ok: boolean };

function emitToast(msg: string, ok = true) {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent<ToastMsg>("console-toast", { detail: { msg, ok } }));
  }
}

export function ConsoleToaster() {
  const [toast, setToast] = useState<ToastMsg | null>(null);
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const onToast = (e: Event) => {
      setToast((e as CustomEvent<ToastMsg>).detail);
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => setToast(null), 3200);
    };
    window.addEventListener("console-toast", onToast);
    return () => {
      window.removeEventListener("console-toast", onToast);
      if (timer) clearTimeout(timer);
    };
  }, []);
  if (!toast) return null;
  return (
    <div
      className={`fixed bottom-6 left-1/2 z-50 -translate-x-1/2 rounded-xl px-4 py-2.5 text-sm font-medium shadow-2xl ${
        toast.ok ? "bg-crown-400 text-slate-950" : "bg-rose-500 text-white"
      }`}
    >
      {toast.msg}
    </div>
  );
}

// ── fetch 封装：非 2xx 抛服务端 error 文案，调用方统一 toast ───────
async function api<T = Record<string, unknown>>(
  url: string,
  init?: RequestInit & { json?: unknown }
): Promise<T> {
  const { json, ...rest } = init ?? {};
  const res = await fetch(url, {
    ...rest,
    headers: { "Content-Type": "application/json", ...(rest.headers ?? {}) },
    ...(json !== undefined ? { body: JSON.stringify(json) } : {}),
  });
  let data: Record<string, unknown> = {};
  try {
    data = await res.json();
  } catch {
    /* 非 JSON 响应按状态码处理 */
  }
  if (!res.ok) {
    throw new Error(String(data?.error ?? `请求失败 (${res.status})`));
  }
  return data as T;
}

const inputCls =
  "w-full rounded-lg border border-slate-700 bg-ink-950 px-3 py-2 text-sm text-slate-200 outline-none placeholder:text-slate-600 focus:border-crown-500";
const btnPrimary =
  "rounded-lg bg-crown-500 px-3 py-2 text-sm font-semibold text-slate-950 hover:bg-crown-400 disabled:opacity-50";
const btnGhost =
  "rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:border-crown-500/60 hover:text-crown-300 disabled:opacity-50";

// ── 登录卡（未登录时由 layout 渲染）────────────────────────────────
// usersEmpty=true → 「初始化主账号」引导表单（CONSOLE_KEY + 用户名 + 密码，建首个 master）；
// 否则 → 用户名 + 密码登录。旧共享口令登录已下线。
export function LoginCard({ configured, usersEmpty }: { configured: boolean; usersEmpty: boolean }) {
  const [key, setKey] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const canSubmit = usersEmpty ? !!(key && username && password) : !!(username && password);

  async function doLogin() {
    if (!canSubmit || busy) return;
    setBusy(true);
    setErr("");
    try {
      await api("/api/console/login", {
        method: "POST",
        json: usersEmpty ? { bootstrap: true, key, username, password } : { username, password },
      });
      location.reload();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setErr(
        msg.includes("invalid key")
          ? "CONSOLE_KEY 不正确"
          : msg.includes("invalid credentials")
            ? "用户名或密码错误"
            : msg.includes("account disabled")
              ? "该账号已被禁用，请联系主账号"
              : msg.includes("too many")
                ? "尝试过多，请 10 分钟后再试"
                : msg.includes("password must be")
                  ? "密码至少 8 位"
                  : msg.includes("username must be")
                    ? "用户名格式：2–32 位小写字母/数字/_.-"
                    : msg
      );
      setBusy(false);
    }
  }

  const onEnter = (e: React.KeyboardEvent) => e.key === "Enter" && doLogin();

  return (
    // 登录页与官网同品牌：ink 深底 + 霓虹青主色 + ∞ 主标（老板要求与 bd2026.cc 一致）；
    // 登录后的内页仍保留琥珀色系（与营销 /admin 青色区分的皇冠资产视觉）。
    <div className="relative mx-auto flex min-h-screen max-w-sm flex-col justify-center bg-ink-950 bg-grid-glow px-6">
      <div className="rounded-2xl border border-cyan-400/25 bg-ink-900/80 p-7 shadow-[0_0_70px_rgba(34,211,238,0.10)] backdrop-blur">
        <div className="mb-3 flex items-center gap-3">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src="/brand/logos/boundless-mark-256.png"
            alt="BOUNDLESS"
            className="h-11 w-11 shrink-0 object-contain drop-shadow-[0_0_14px_rgba(34,211,238,0.45)]"
            draggable={false}
          />
          <div className="leading-tight">
            <span className="block text-lg font-bold text-white">无界 · 集团控制台</span>
            <span className="block text-[11px] font-semibold uppercase tracking-[0.2em] text-cyan-400/90">
              Boundless Console
            </span>
          </div>
        </div>
        <div className="inline-flex items-center gap-1.5 rounded-full border border-cyan-400/30 bg-cyan-400/10 px-2.5 py-1 text-[11px] font-medium text-cyan-300">
          <ShieldAlert className="h-3.5 w-3.5" />
          皇冠资产 · 最小暴露
        </div>
        {usersEmpty ? (
          <>
            <p className="mb-4 mt-4 text-xs leading-relaxed text-slate-400">
              <span className="font-semibold text-cyan-300">初始化主账号</span>
              ：控制台尚无任何账号。下面 <span className="font-semibold text-white">三项均为必填</span>
              （口令验证身份 + 你自己设定的用户名和密码），填齐后按钮才会亮起。
            </p>
            {configured ? (
              <div className="space-y-2.5">
                <div className="relative">
                  <Lock className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
                  <input
                    type="password"
                    value={key}
                    onChange={(e) => setKey(e.target.value)}
                    placeholder="CONSOLE_KEY（服务端口令）"
                    className={`${inputCls} py-2.5 pl-9`}
                    onKeyDown={onEnter}
                    autoFocus
                  />
                </div>
                <input
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="主账号用户名（小写字母/数字/_.-）"
                  className={`${inputCls} py-2.5`}
                  onKeyDown={onEnter}
                  autoComplete="username"
                />
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="密码（至少 8 位）"
                  className={`${inputCls} py-2.5`}
                  onKeyDown={onEnter}
                  autoComplete="new-password"
                />
                <button onClick={doLogin} disabled={busy || !canSubmit} className={`${btnPrimary} w-full py-2.5`}>
                  {busy ? "创建中…" : canSubmit ? "创建主账号并进入" : "填齐三项后可创建"}
                </button>
              </div>
            ) : (
              <p className="rounded-lg border border-rose-500/30 bg-rose-950/30 p-3 text-xs leading-relaxed text-rose-300">
                服务端未配置口令：请在 .env.local 设置 CONSOLE_KEY（生产必须独立设置，勿与
                ADMIN_KEY 共用），重启后再来初始化主账号。
              </p>
            )}
          </>
        ) : (
          <>
            <p className="mb-4 mt-4 text-xs leading-relaxed text-slate-500">
              管理集团客户、订单与授权台账。请用控制台账号登录。
            </p>
            <div className="space-y-2.5">
              <div className="relative">
                <UserRound className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
                <input
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="用户名"
                  className={`${inputCls} py-2.5 pl-9`}
                  onKeyDown={onEnter}
                  autoComplete="username"
                  autoFocus
                />
              </div>
              <div className="relative">
                <Lock className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="密码"
                  className={`${inputCls} py-2.5 pl-9`}
                  onKeyDown={onEnter}
                  autoComplete="current-password"
                />
              </div>
              <button onClick={doLogin} disabled={busy || !canSubmit} className={`${btnPrimary} w-full py-2.5`}>
                {busy ? "登录中…" : "进入控制台"}
              </button>
            </div>
          </>
        )}
        {err && <p className="mt-3 text-center text-sm text-rose-400">{err}</p>}
      </div>
      <p className="mt-4 text-center text-[11px] text-slate-600">
        实名账号 + RBAC（viewer / admin / master）；生产请配合 IP 白名单使用。
      </p>
    </div>
  );
}

export function LogoutButton() {
  const [busy, setBusy] = useState(false);
  return (
    <button
      onClick={async () => {
        setBusy(true);
        try {
          await api("/api/console/logout", { method: "POST" });
        } catch {
          /* 清 cookie 失败也照样刷新回登录页 */
        }
        location.reload();
      }}
      disabled={busy}
      className="flex items-center gap-1 rounded-lg border border-slate-700 px-2.5 py-1.5 text-xs text-slate-400 hover:border-rose-500 hover:text-rose-300 disabled:opacity-50"
    >
      <LogOut className="h-3.5 w-3.5" />
      登出
    </button>
  );
}

// ── 顶部导航（四组分区：经营 / 增长 / 平台 / 系统；用户管理入口仅 master 可见）──
// 分组语义：经营 = 每天看的钱与客户；增长 = 获客与转化侧台账；平台 = 集团级
// 数字资产与遥测；系统 = 审计与运维。当前路由高亮沿用 startsWith（/console 精确）。
interface NavItem {
  href: string;
  label: string;
  Icon: typeof LayoutDashboard;
}

const NAV_GROUPS: { label: string; items: NavItem[] }[] = [
  {
    label: "经营",
    items: [
      { href: "/console", label: "总览", Icon: LayoutDashboard },
      { href: "/console/customers", label: "客户", Icon: Users },
      { href: "/console/orders", label: "订单", Icon: ReceiptText },
      { href: "/console/licenses", label: "授权", Icon: KeyRound },
      { href: "/console/opportunities", label: "商机", Icon: Sparkles },
    ],
  },
  {
    label: "增长",
    items: [
      { href: "/console/leads", label: "留资", Icon: Inbox },
      { href: "/console/trial", label: "试用", Icon: Gift },
      { href: "/console/funnel", label: "激活漏斗", Icon: Filter },
      { href: "/console/channels", label: "渠道", Icon: Radio },
    ],
  },
  {
    label: "平台",
    items: [
      { href: "/console/personas", label: "人设", Icon: VenetianMask },
      { href: "/console/kpi", label: "事件流", Icon: BarChart3 },
    ],
  },
  {
    label: "系统",
    items: [
      { href: "/console/audit", label: "审计", Icon: ScrollText },
      { href: "/console/errors", label: "错误", Icon: Bug },
    ],
  },
];

const NAV_USERS: NavItem = { href: "/console/users", label: "用户", Icon: UserCog };

export function ConsoleNav({ showUsers = false }: { showUsers?: boolean }) {
  const pathname = usePathname();
  const groups = showUsers
    ? NAV_GROUPS.map((g) => (g.label === "系统" ? { ...g, items: [...g.items, NAV_USERS] } : g))
    : NAV_GROUPS;
  return (
    <nav className="flex items-center gap-0.5 overflow-x-auto">
      {groups.map((g, gi) => (
        <div key={g.label} className="flex shrink-0 items-center gap-0.5">
          {gi > 0 && <span aria-hidden className="mx-1.5 h-4 w-px shrink-0 bg-ink-700" />}
          <span className="hidden shrink-0 select-none pr-1 text-[10px] font-semibold uppercase tracking-wider text-slate-600 lg:inline">
            {g.label}
          </span>
          {g.items.map(({ href, label, Icon }) => {
            const active = href === "/console" ? pathname === "/console" : pathname?.startsWith(href);
            return (
              <Link
                key={href}
                href={href}
                className={`flex shrink-0 items-center gap-1.5 border-b-2 px-2.5 py-2.5 text-sm font-medium transition ${
                  active
                    ? "border-crown-400 text-crown-300"
                    : "border-transparent text-slate-400 hover:text-slate-200"
                }`}
              >
                <Icon className="h-4 w-4" />
                {label}
              </Link>
            );
          })}
        </div>
      ))}
    </nav>
  );
}

// ── 新建客户（客户页 + 归属控件内复用的提交逻辑）───────────────────
interface CreatedCustomer {
  id: string;
  display_name: string | null;
}

async function createCustomerReq(input: {
  display_name: string;
  primary_contact?: string;
  notes?: string;
  identity?: { kind: string; value: string };
}): Promise<CreatedCustomer> {
  const data = await api<{ customer: CreatedCustomer }>("/api/console/customers", {
    method: "POST",
    json: input,
  });
  return data.customer;
}

const IDENTITY_KIND_OPTIONS = [
  { value: "contact", label: "contact 通用联系" },
  { value: "tg", label: "tg Telegram" },
  { value: "email", label: "email 邮箱" },
  { value: "phone", label: "phone 电话" },
  { value: "fingerprint", label: "fingerprint 设备指纹" },
] as const;

export function NewCustomerForm() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState("");
  const [contact, setContact] = useState("");
  const [notes, setNotes] = useState("");
  const [identKind, setIdentKind] = useState("contact");
  const [identValue, setIdentValue] = useState("");

  async function submit() {
    if (!name.trim() || busy) return;
    setBusy(true);
    try {
      const customer = await createCustomerReq({
        display_name: name.trim(),
        primary_contact: contact.trim() || undefined,
        notes: notes.trim() || undefined,
        identity: identValue.trim() ? { kind: identKind, value: identValue.trim() } : undefined,
      });
      emitToast(`已创建客户 ${customer.display_name ?? customer.id}`);
      setOpen(false);
      setName("");
      setContact("");
      setNotes("");
      setIdentValue("");
      router.refresh();
    } catch (e) {
      emitToast(e instanceof Error ? e.message : String(e), false);
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button onClick={() => setOpen(true)} className={`${btnPrimary} flex items-center gap-1.5`}>
        <Plus className="h-4 w-4" />
        新建客户
      </button>
    );
  }
  return (
    <div className="w-full rounded-xl border border-crown-500/25 bg-ink-900/70 p-4 sm:max-w-md">
      <div className="mb-3 flex items-center justify-between">
        <span className="text-sm font-semibold text-crown-300">新建客户</span>
        <button onClick={() => setOpen(false)} className="text-slate-500 hover:text-slate-300" aria-label="关闭">
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="space-y-2.5">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="显示名 *（如：张总 / ACME Ltd.）" className={inputCls} />
        <input value={contact} onChange={(e) => setContact(e.target.value)} placeholder="主联系方式（微信 / TG / 邮箱，可空）" className={inputCls} />
        <input value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="备注（可空）" className={inputCls} />
        <div className="flex gap-2">
          <select value={identKind} onChange={(e) => setIdentKind(e.target.value)} className={`${inputCls} w-40`}>
            {IDENTITY_KIND_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
          <input
            value={identValue}
            onChange={(e) => setIdentValue(e.target.value)}
            placeholder="初始身份标识值（可空，用于自动归属）"
            className={inputCls}
          />
        </div>
        <button onClick={submit} disabled={busy || !name.trim()} className={`${btnPrimary} w-full`}>
          {busy ? "创建中…" : "创建客户"}
        </button>
      </div>
    </div>
  );
}

// ── 客户搜索选择器（共享）───────────────────────────────────────────
// 归属类控件的公共内核：输入即异步搜（250ms 防抖 → GET /api/console/customers?q=）
// 只取前 20 条候选。替代旧「服务端预灌 500 客户进 <select>」——客户量增长后
// 预灌既慢又不可用（原生 select 无法搜索）。
export interface CustomerOption {
  id: string;
  label: string;
}

interface CustomerApiRow {
  id: string;
  display_name: string | null;
  primary_contact: string | null;
}

function customerLabel(c: CustomerApiRow): string {
  return `${c.display_name || "（未命名）"}${c.primary_contact ? ` · ${c.primary_contact}` : ""}`;
}

export function CustomerPicker({
  onPick,
  disabled = false,
}: {
  onPick: (customer: CustomerOption) => void | Promise<void>;
  disabled?: boolean;
}) {
  const [q, setQ] = useState("");
  const [items, setItems] = useState<CustomerOption[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    const t = setTimeout(async () => {
      try {
        const d = await api<{ rows: CustomerApiRow[] }>(
          `/api/console/customers?q=${encodeURIComponent(q.trim())}&limit=20`
        );
        if (alive) setItems(d.rows.map((c) => ({ id: c.id, label: customerLabel(c) })));
      } catch {
        if (alive) setItems([]);
      } finally {
        if (alive) setLoading(false);
      }
    }, 250);
    return () => {
      alive = false;
      clearTimeout(t);
    };
  }, [q]);

  return (
    <div className="space-y-1.5">
      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="搜索客户名 / 联系方式 / TG ID"
        className={`${inputCls} px-2 py-1.5 text-xs`}
        autoFocus
        disabled={disabled}
      />
      <div className="max-h-44 space-y-0.5 overflow-y-auto">
        {loading ? (
          <p className="p-1 text-[11px] text-slate-500">搜索中…</p>
        ) : items.length === 0 ? (
          <p className="p-1 text-[11px] leading-relaxed text-slate-500">
            {q.trim() ? "没有匹配的客户，切到「快捷新建」直接建一位。" : "还没有客户，切到「快捷新建」直接建一位。"}
          </p>
        ) : (
          items.map((c) => (
            <button
              key={c.id}
              onClick={() => onPick(c)}
              disabled={disabled}
              className="block w-full truncate rounded-md px-2 py-1.5 text-left text-xs text-slate-200 hover:bg-crown-500/10 hover:text-crown-300 disabled:opacity-50"
              title={c.label}
            >
              {c.label}
            </button>
          ))
        )}
      </div>
    </div>
  );
}

// ── 归属客户（订单/授权/留资行内控件；已归属行用 assigned 出「改绑/解绑」）──
export function AssignCustomerControl({
  entity,
  entityKey,
  assigned = false,
}: {
  entity: "order" | "license" | "lead";
  entityKey: string;
  /** true = 该行已归属：触发钮变低调的「改绑」，面板里追加「解除归属」。 */
  assigned?: boolean;
}) {
  const router = useRouter();
  const [mode, setMode] = useState<"closed" | "pick" | "create">("closed");
  const [busy, setBusy] = useState(false);
  const [newName, setNewName] = useState("");
  const [newContact, setNewContact] = useState("");

  const endpoint =
    entity === "order" ? "/api/console/orders" : entity === "license" ? "/api/console/licenses" : "/api/console/leads";

  async function patchAssign(customerId: string | null) {
    const body =
      entity === "lead" ? { source_key: entityKey, customer_id: customerId } : { id: entityKey, customer_id: customerId };
    await api(endpoint, { method: "PATCH", json: body });
  }

  async function pick(c: CustomerOption) {
    if (busy) return;
    setBusy(true);
    try {
      await patchAssign(c.id);
      emitToast(`已归属 ${c.label}`);
      setMode("closed");
      router.refresh();
    } catch (e) {
      emitToast(e instanceof Error ? e.message : String(e), false);
    } finally {
      setBusy(false);
    }
  }

  async function submitCreate() {
    if (!newName.trim() || busy) return;
    setBusy(true);
    try {
      const customer = await createCustomerReq({
        display_name: newName.trim(),
        primary_contact: newContact.trim() || undefined,
      });
      await patchAssign(customer.id);
      emitToast(`已创建并归属 ${customer.display_name ?? customer.id}`);
      setMode("closed");
      setNewName("");
      setNewContact("");
      router.refresh();
    } catch (e) {
      emitToast(e instanceof Error ? e.message : String(e), false);
    } finally {
      setBusy(false);
    }
  }

  async function submitUnassign() {
    if (busy) return;
    setBusy(true);
    try {
      await patchAssign(null);
      emitToast("已解除归属");
      setMode("closed");
      router.refresh();
    } catch (e) {
      emitToast(e instanceof Error ? e.message : String(e), false);
    } finally {
      setBusy(false);
    }
  }

  if (mode === "closed") {
    return assigned ? (
      <button
        onClick={() => setMode("pick")}
        title="改绑到别的客户，或解除归属"
        className="inline-flex items-center gap-1 rounded-md border border-slate-700 px-1.5 py-0.5 text-[11px] text-slate-500 hover:border-crown-500/50 hover:text-crown-300"
      >
        <Link2 className="h-3 w-3" />
        改绑
      </button>
    ) : (
      <button
        onClick={() => setMode("pick")}
        className="inline-flex items-center gap-1 rounded-lg border border-crown-500/40 px-2 py-1 text-xs font-medium text-crown-300 hover:bg-crown-500/10"
      >
        <Link2 className="h-3 w-3" />
        归属客户
      </button>
    );
  }

  return (
    <div className="w-64 rounded-lg border border-slate-700 bg-ink-900 p-2 text-left shadow-xl">
      <div className="mb-1.5 flex items-center justify-between">
        <div className="flex gap-1 text-[11px]">
          <button
            onClick={() => setMode("pick")}
            className={`rounded px-1.5 py-0.5 ${mode === "pick" ? "bg-crown-500/20 text-crown-300" : "text-slate-400 hover:text-slate-200"}`}
          >
            搜客户
          </button>
          <button
            onClick={() => setMode("create")}
            className={`rounded px-1.5 py-0.5 ${mode === "create" ? "bg-crown-500/20 text-crown-300" : "text-slate-400 hover:text-slate-200"}`}
          >
            快捷新建
          </button>
        </div>
        <button onClick={() => setMode("closed")} className="text-slate-500 hover:text-slate-300" aria-label="关闭">
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
      {mode === "pick" ? (
        <CustomerPicker onPick={pick} disabled={busy} />
      ) : (
        <div className="space-y-1.5">
          <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="客户显示名 *" className={`${inputCls} px-2 py-1.5 text-xs`} />
          <input value={newContact} onChange={(e) => setNewContact(e.target.value)} placeholder="联系方式（可空）" className={`${inputCls} px-2 py-1.5 text-xs`} />
          <button onClick={submitCreate} disabled={busy || !newName.trim()} className={`${btnPrimary} w-full px-2 py-1.5 text-xs`}>
            {busy ? "提交中…" : "创建并归属"}
          </button>
        </div>
      )}
      {assigned && (
        <button
          onClick={submitUnassign}
          disabled={busy}
          className="mt-1.5 w-full rounded-lg border border-rose-500/40 px-2 py-1.5 text-xs text-rose-300 hover:bg-rose-500/10 disabled:opacity-50"
        >
          {busy ? "处理中…" : "解除归属（归错人时用）"}
        </button>
      )}
    </div>
  );
}

// ── 客户 360：编辑主档（display_name / primary_contact / notes）────────
export function EditCustomerForm({
  customerId,
  initial,
}: {
  customerId: string;
  initial: { display_name: string | null; primary_contact: string | null; notes: string | null };
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState(initial.display_name ?? "");
  const [contact, setContact] = useState(initial.primary_contact ?? "");
  const [notes, setNotes] = useState(initial.notes ?? "");

  async function submit() {
    if (!name.trim() || busy) return;
    setBusy(true);
    try {
      await api(`/api/console/customers/${customerId}`, {
        method: "PATCH",
        json: { display_name: name.trim(), primary_contact: contact.trim() || null, notes: notes.trim() || null },
      });
      emitToast("主档已更新");
      setOpen(false);
      router.refresh();
    } catch (e) {
      emitToast(e instanceof Error ? e.message : String(e), false);
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button onClick={() => setOpen(true)} className={`${btnGhost} px-2.5 py-1 text-xs`}>
        编辑主档
      </button>
    );
  }
  return (
    <div className="w-full rounded-xl border border-crown-500/25 bg-ink-900/70 p-3">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs font-semibold text-crown-300">编辑主档</span>
        <button onClick={() => setOpen(false)} className="text-slate-500 hover:text-slate-300" aria-label="关闭">
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
      <div className="space-y-2">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="显示名 *" className={inputCls} />
        <input value={contact} onChange={(e) => setContact(e.target.value)} placeholder="主联系方式（可空）" className={inputCls} />
        <input value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="备注（可空）" className={inputCls} />
        <button onClick={submit} disabled={busy || !name.trim()} className={`${btnPrimary} w-full`}>
          {busy ? "保存中…" : "保存"}
        </button>
      </div>
    </div>
  );
}

// ── 客户 360：解绑身份（两段确认，防误点）──────────────────────────
export function DetachIdentityButton({ customerId, identityId }: { customerId: string; identityId: number }) {
  const router = useRouter();
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);

  async function doDetach() {
    if (busy) return;
    setBusy(true);
    try {
      await api(`/api/console/customers/${customerId}`, {
        method: "POST",
        json: { action: "detach_identity", identity_id: identityId },
      });
      emitToast("身份已解绑（历史归属不回滚）");
      router.refresh();
    } catch (e) {
      emitToast(e instanceof Error ? e.message : String(e), false);
      setBusy(false);
      setArmed(false);
    }
  }

  if (!armed) {
    return (
      <button
        onClick={() => setArmed(true)}
        title="解绑该身份（之后新订单/留资不再按它自动归属；历史归属保留）"
        className="text-slate-600 hover:text-rose-300"
        aria-label="解绑身份"
      >
        <X className="h-3.5 w-3.5" />
      </button>
    );
  }
  return (
    <span className="inline-flex items-center gap-1">
      <button
        onClick={doDetach}
        disabled={busy}
        className="rounded border border-rose-500/40 px-1.5 py-0.5 text-[10px] font-medium text-rose-300 hover:bg-rose-500/10 disabled:opacity-50"
      >
        {busy ? "…" : "确认解绑"}
      </button>
      <button onClick={() => setArmed(false)} disabled={busy} className="text-slate-500 hover:text-slate-300" aria-label="取消">
        <X className="h-3 w-3" />
      </button>
    </span>
  );
}

// ── 用户管理（/console/users；API 侧全部动作仅 master）─────────────
export interface ConsoleUserItem {
  id: string;
  username: string;
  role: "master" | "admin" | "viewer";
  display_name: string | null;
  enabled: boolean;
  created_at: string | null;
  last_login: string | null;
}

const ROLE_OPTIONS = [
  { value: "viewer", label: "viewer 只读" },
  { value: "admin", label: "admin 运营" },
  { value: "master", label: "master 主账号" },
] as const;

export function NewUserForm() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("viewer");
  const [displayName, setDisplayName] = useState("");

  async function submit() {
    if (!username.trim() || !password || busy) return;
    setBusy(true);
    try {
      await api("/api/console/users", {
        method: "POST",
        json: {
          username: username.trim(),
          password,
          role,
          display_name: displayName.trim() || undefined,
        },
      });
      emitToast(`已创建用户 ${username.trim()}`);
      setOpen(false);
      setUsername("");
      setPassword("");
      setRole("viewer");
      setDisplayName("");
      router.refresh();
    } catch (e) {
      emitToast(e instanceof Error ? e.message : String(e), false);
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button onClick={() => setOpen(true)} className={`${btnPrimary} flex items-center gap-1.5`}>
        <Plus className="h-4 w-4" />
        新建用户
      </button>
    );
  }
  return (
    <div className="w-full rounded-xl border border-crown-500/25 bg-ink-900/70 p-4 sm:max-w-md">
      <div className="mb-3 flex items-center justify-between">
        <span className="text-sm font-semibold text-crown-300">新建用户</span>
        <button onClick={() => setOpen(false)} className="text-slate-500 hover:text-slate-300" aria-label="关闭">
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="space-y-2.5">
        <input
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          placeholder="用户名 *（2–32 位小写字母/数字/_.-）"
          className={inputCls}
          autoComplete="off"
        />
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="初始密码 *（至少 8 位）"
          className={inputCls}
          autoComplete="new-password"
        />
        <div className="flex gap-2">
          <select value={role} onChange={(e) => setRole(e.target.value)} className={`${inputCls} w-44`}>
            {ROLE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
          <input
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder="显示名（可空）"
            className={inputCls}
          />
        </div>
        <button onClick={submit} disabled={busy || !username.trim() || !password} className={`${btnPrimary} w-full`}>
          {busy ? "创建中…" : "创建用户"}
        </button>
      </div>
    </div>
  );
}

/** 用户行操作：改角色 / 禁用启用 / 重置密码。isLastEnabledMaster 时 UI 直接禁用
 *  降级与禁用入口（API 侧仍有硬校验兜底）。 */
export function UserActions({ user, isLastEnabledMaster }: { user: ConsoleUserItem; isLastEnabledMaster: boolean }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [pwOpen, setPwOpen] = useState(false);
  const [pw, setPw] = useState("");

  async function patch(payload: Record<string, unknown>, okMsg: string) {
    if (busy) return;
    setBusy(true);
    try {
      await api("/api/console/users", { method: "PATCH", json: { id: user.id, ...payload } });
      emitToast(okMsg);
      router.refresh();
    } catch (e) {
      emitToast(e instanceof Error ? e.message : String(e), false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <select
        value={user.role}
        disabled={busy || isLastEnabledMaster}
        onChange={(e) => patch({ action: "set_role", role: e.target.value }, `已把 ${user.username} 改为 ${e.target.value}`)}
        className={`${inputCls} w-auto px-2 py-1 text-xs`}
        title={isLastEnabledMaster ? "最后一个启用的 master 不能降级" : "改角色"}
      >
        {ROLE_OPTIONS.map((o) => (
          <option key={o.value} value={o.value}>{o.value}</option>
        ))}
      </select>
      <button
        disabled={busy || (user.enabled && isLastEnabledMaster)}
        onClick={() =>
          patch({ action: "set_enabled", enabled: !user.enabled }, user.enabled ? `已禁用 ${user.username}（会话已撤销）` : `已启用 ${user.username}`)
        }
        className={`rounded-lg border px-2 py-1 text-xs disabled:opacity-40 ${
          user.enabled
            ? "border-rose-500/40 text-rose-300 hover:bg-rose-500/10"
            : "border-emerald-500/40 text-emerald-300 hover:bg-emerald-500/10"
        }`}
        title={user.enabled && isLastEnabledMaster ? "最后一个启用的 master 不能禁用" : undefined}
      >
        {user.enabled ? "禁用" : "启用"}
      </button>
      {pwOpen ? (
        <span className="flex items-center gap-1.5">
          <input
            type="password"
            value={pw}
            onChange={(e) => setPw(e.target.value)}
            placeholder="新密码（≥8 位）"
            className={`${inputCls} w-36 px-2 py-1 text-xs`}
            autoComplete="new-password"
            onKeyDown={(e) => {
              if (e.key === "Enter" && pw.length >= 8) {
                patch({ action: "reset_password", password: pw }, `已重置 ${user.username} 的密码（旧会话已撤销）`);
                setPwOpen(false);
                setPw("");
              }
            }}
          />
          <button
            disabled={busy || pw.length < 8}
            onClick={() => {
              patch({ action: "reset_password", password: pw }, `已重置 ${user.username} 的密码（旧会话已撤销）`);
              setPwOpen(false);
              setPw("");
            }}
            className={`${btnGhost} px-2 py-1 text-xs`}
          >
            确认
          </button>
          <button
            onClick={() => {
              setPwOpen(false);
              setPw("");
            }}
            className="text-slate-500 hover:text-slate-300"
            aria-label="取消重置"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </span>
      ) : (
        <button disabled={busy} onClick={() => setPwOpen(true)} className={`${btnGhost} px-2 py-1 text-xs`}>
          重置密码
        </button>
      )}
    </div>
  );
}

// ── 商机跟进（schema v4；自 opportunities-ui.tsx 合并进来，api/emitToast 单源）──
// 状态小徽章 + 「跟进/赢单/忽略」行尾操作（带确认面板与备注小输入）；已关闭
// （won/dismissed）的行提供「重开」。admin+ 才由服务端页面渲染操作件；提交走
// POST /api/console/opportunities → markOpportunity，成功后 router.refresh()。
// 备注隐私纪律：note 只写运营自己的跟进话术/结果，绝不粘贴客户聊天原文。
export type OppLogStatus = "open" | "contacted" | "won" | "dismissed";

export interface OppLogInfo {
  status: OppLogStatus;
  note: string | null;
  acted_by: string | null;
  acted_at: string | null;
}

const OPP_LOG_STATUS_STYLE: Record<OppLogStatus, string> = {
  open: "bg-slate-500/15 text-slate-300 border-slate-500/30",
  contacted: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  won: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  dismissed: "bg-slate-500/15 text-slate-400 border-slate-500/30",
};
const OPP_LOG_STATUS_LABEL: Record<OppLogStatus, string> = {
  open: "待跟进",
  contacted: "已联系",
  won: "已赢单",
  dismissed: "已忽略",
};

/** 商机行的跟进状态徽章：未标记（log=null）不占位；title 带经手人/时间/备注。 */
export function OpportunityLogBadge({ log }: { log: OppLogInfo | null }) {
  if (!log) return null;
  const title = [
    log.acted_by ? `经手：${log.acted_by}` : null,
    log.acted_at ? `时间：${log.acted_at.slice(0, 16).replace("T", " ")}` : null,
    log.note ? `备注：${log.note}` : null,
  ]
    .filter(Boolean)
    .join("\n");
  return (
    <span
      title={title || undefined}
      className={`inline-block shrink-0 rounded-full border px-2 py-0.5 text-[11px] font-medium ${OPP_LOG_STATUS_STYLE[log.status]}`}
    >
      {OPP_LOG_STATUS_LABEL[log.status]}
    </span>
  );
}

interface OppAction {
  status: OppLogStatus;
  label: string;
  cls: string;
  confirmLabel: string;
}

const OPP_ACTIONS: OppAction[] = [
  {
    status: "contacted",
    label: "跟进",
    cls: "border-sky-500/40 text-sky-300 hover:bg-sky-500/10",
    confirmLabel: "标记为「已联系」（保留在清单，信号值 −20）",
  },
  {
    status: "won",
    label: "赢单",
    cls: "border-emerald-500/40 text-emerald-300 hover:bg-emerald-500/10",
    confirmLabel: "标记为「已赢单」（默认从清单隐藏）",
  },
  {
    status: "dismissed",
    label: "忽略",
    cls: "border-slate-600 text-slate-400 hover:border-rose-500/50 hover:text-rose-300",
    confirmLabel: "标记为「已忽略」（默认从清单隐藏）",
  },
];

const OPP_REOPEN: OppAction = {
  status: "open",
  label: "重开",
  cls: "border-crown-500/40 text-crown-300 hover:bg-crown-500/10",
  confirmLabel: "重新打开（回到「待跟进」清单，信号值恢复）",
};

export function OpportunityActions({
  oppKey,
  kind,
  customerId,
  toProduct,
  log,
}: {
  oppKey: string;
  kind: string;
  customerId: string;
  toProduct?: string | null;
  log: OppLogInfo | null;
}) {
  const router = useRouter();
  const [pending, setPending] = useState<OppLogStatus | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  // 已关闭（won/dismissed）只出「重开」；进行中出三个跟进动作。
  const closed = log?.status === "won" || log?.status === "dismissed";
  const actions = closed ? [OPP_REOPEN] : OPP_ACTIONS;

  async function submit(status: OppLogStatus) {
    if (busy) return;
    setBusy(true);
    try {
      await api("/api/console/opportunities", {
        method: "POST",
        json: {
          opp_key: oppKey,
          kind,
          customer_id: customerId,
          to_product: toProduct ?? undefined,
          status,
          // 空备注不传 → 保留已有备注（重复标记不冲掉上次跟进语）
          ...(note.trim() ? { note: note.trim() } : {}),
        },
      });
      emitToast(`已标记：${OPP_LOG_STATUS_LABEL[status]}`);
      setPending(null);
      setNote("");
      router.refresh();
    } catch (e) {
      emitToast(e instanceof Error ? e.message : String(e), false);
    } finally {
      setBusy(false);
    }
  }

  if (pending) {
    const action = actions.find((a) => a.status === pending) ?? OPP_REOPEN;
    return (
      <div className="w-60 rounded-lg border border-slate-700 bg-ink-900 p-2 text-left shadow-xl">
        <div className="mb-1.5 flex items-start justify-between gap-2">
          <p className="text-[11px] leading-relaxed text-slate-300">{action.confirmLabel}</p>
          <button
            onClick={() => {
              setPending(null);
              setNote("");
            }}
            className="shrink-0 text-slate-500 hover:text-slate-300"
            aria-label="取消"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="备注（可空；只写跟进结果，勿贴聊天原文）"
          className={`${inputCls} px-2 py-1.5 text-xs`}
          onKeyDown={(e) => e.key === "Enter" && submit(pending)}
          autoFocus
        />
        <button
          onClick={() => submit(pending)}
          disabled={busy}
          className={`${btnPrimary} mt-1.5 w-full px-2 py-1.5 text-xs`}
        >
          {busy ? "提交中…" : "确认"}
        </button>
      </div>
    );
  }

  return (
    <span className="inline-flex gap-1">
      {actions.map((a) => (
        <button
          key={a.status}
          onClick={() => setPending(a.status)}
          disabled={busy || log?.status === a.status}
          title={log?.status === a.status ? `当前已是「${OPP_LOG_STATUS_LABEL[a.status]}」` : a.confirmLabel}
          className={`rounded-md border px-1.5 py-0.5 text-[11px] font-medium disabled:cursor-not-allowed disabled:opacity-40 ${a.cls}`}
        >
          {a.label}
        </button>
      ))}
    </span>
  );
}

// ── 客户 360：挂身份标识 ───────────────────────────────────────────
export function AttachIdentityForm({ customerId }: { customerId: string }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [kind, setKind] = useState("contact");
  const [value, setValue] = useState("");

  async function submit() {
    if (!value.trim() || busy) return;
    setBusy(true);
    try {
      await api(`/api/console/customers/${customerId}`, {
        method: "POST",
        json: { action: "attach_identity", kind, value: value.trim() },
      });
      emitToast("身份标识已挂接");
      setValue("");
      router.refresh();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      emitToast(msg.includes("another customer") ? "该身份已属于另一位客户，未抢占" : msg, false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-wrap gap-2">
      <select value={kind} onChange={(e) => setKind(e.target.value)} className={`${inputCls} w-44`}>
        {IDENTITY_KIND_OPTIONS.map((o) => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
      <input
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="标识值（如 tg 用户 ID / 邮箱）"
        className={`${inputCls} min-w-[180px] flex-1`}
        onKeyDown={(e) => e.key === "Enter" && submit()}
      />
      <button onClick={submit} disabled={busy || !value.trim()} className={btnGhost}>
        {busy ? "挂接中…" : "＋ 挂身份"}
      </button>
    </div>
  );
}

// ── 挽回名单：标记已联系 + 一键复制（P3-⑬；/console/funnel）──────────────────
// 「已联系」落 winback-outreach 台账避免重复外呼；复制走客户端 TSV（零新路由）。
export interface WinbackClientRow {
  fp: string;
  contact: string;
  contactKind: string;
  claimedAt: string;
  ver: string;
  capable: boolean;
  dispatched: boolean;
  contactedAt?: string;
  contactedBy?: string;
}

export function WinbackContactButton({ fp, contacted }: { fp: string; contacted: boolean }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  async function toggle() {
    if (busy) return;
    setBusy(true);
    try {
      await api("/api/console/funnel/winback-contacted", {
        method: "POST",
        json: { fingerprint: fp, undo: contacted },
      });
      emitToast(contacted ? "已恢复为未联系" : "已标记为已联系");
      router.refresh();
    } catch (e) {
      emitToast(e instanceof Error ? e.message : String(e), false);
    } finally {
      setBusy(false);
    }
  }
  return (
    <button
      onClick={toggle}
      disabled={busy}
      className={`rounded border px-1.5 py-0.5 text-[10px] disabled:opacity-50 ${
        contacted
          ? "border-slate-600 text-slate-400 hover:bg-slate-500/10"
          : "border-emerald-500/40 text-emerald-300 hover:bg-emerald-500/10"
      }`}
    >
      {busy ? "…" : contacted ? "撤销" : "标记已联系"}
    </button>
  );
}

export function WinbackCopyButton({ rows }: { rows: WinbackClientRow[] }) {
  const [done, setDone] = useState(false);
  function copy() {
    // TSV：贴进表格/群直接可读；只含运营外呼所需字段，不含任何聊天内容
    const header = ["联系方式", "类型", "领取时间", "指纹", "版本", "已派发", "已联系"];
    const body = rows.map((r) => [
      r.contact, r.contactKind, r.claimedAt, r.fp, r.ver || "?",
      r.dispatched ? "是" : "否", r.contactedAt ? "是" : "否",
    ].join("\t"));
    const tsv = [header.join("\t"), ...body].join("\n");
    const fallback = () => {
      const ta = document.createElement("textarea");
      ta.value = tsv;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch { /* ignore */ }
      document.body.removeChild(ta);
    };
    const p = navigator.clipboard?.writeText?.(tsv);
    if (p && typeof p.then === "function") p.then(() => {}, fallback);
    else fallback();
    setDone(true);
    emitToast(`已复制 ${rows.length} 行到剪贴板`);
    setTimeout(() => setDone(false), 2000);
  }
  return (
    <button
      onClick={copy}
      disabled={!rows.length}
      className="rounded border border-ink-600 px-2 py-1 text-[11px] text-slate-300 hover:bg-ink-700 disabled:opacity-50"
    >
      {done ? "已复制 ✓" : `复制全部（${rows.length}）`}
    </button>
  );
}

// ── 凭据组解除隔离（P2；试用页凭据池条）────────────────────────────────────
// 隔离由客户端举报聚类自动触发；解除是人的决定——按钮文案提醒先跑探针核实，
// 误报才解除（真废组解除了也只会被再次举报隔离，白折腾用户一轮）。
export function UnquarantineButton({ apiId, name }: { apiId: string; name: string }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [armed, setArmed] = useState(false);
  async function go() {
    if (busy) return;
    if (!armed) {
      setArmed(true);
      emitToast(`再点一次确认解除 ${name} 的隔离（请先用 tg_cred_probe 核实为误报）`, true);
      setTimeout(() => setArmed(false), 4000);
      return;
    }
    setBusy(true);
    try {
      await api("/api/console/pool/unquarantine", { method: "POST", json: { api_id: apiId } });
      emitToast(`已解除 ${name} 的隔离，恢复派发`);
      router.refresh();
    } catch (e) {
      emitToast(e instanceof Error ? e.message : String(e), false);
    } finally {
      setBusy(false);
      setArmed(false);
    }
  }
  return (
    <button
      onClick={go}
      disabled={busy}
      className="rounded border border-amber-500/40 px-1.5 py-0.5 text-[10px] text-amber-300 hover:bg-amber-500/10 disabled:opacity-50"
      title="先用 tg_cred_probe 核实为误报再解除"
    >
      {busy ? "解除中…" : armed ? "确认解除?" : "解除隔离"}
    </button>
  );
}
